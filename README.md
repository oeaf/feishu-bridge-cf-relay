# 飞书桥完整搭建流程：从创建到正常使用

> 一份照着做就能复现的完整流程。覆盖飞书后台建应用、代码、Cloudflare Worker 中转、服务器部署、沙箱侧接线、看护、验收、日常使用、多桥与回滚。**不含任何真实凭证值**：App Secret、中转 token、API Key 一律只写“从哪里取、放哪里、什么权限”，不写值本身。
>
> 实测基线：2026-11-25，中国飞书，单桥部署。首条真实消息收发全通，全程约 1 分半（30 秒轮询档）。

---

## 一、这是什么

一个飞书企业自建应用机器人，接到 Agent。用户在飞书私聊里发文字、图片、视频、文件，Agent 处理后把回复发回飞书。

**为什么是这个形态（三句话）：**

1. 沙箱不适合跑 24 小时监听。2026-11-18 13:40 之后一周零进账；daemon 连上飞书 WS 几分钟就掉线，掉线后自己不重连；supervisor 在 20 小时内重启了 11 次（沙箱替换专杀后台进程）。心跳只写 pid 和 uptime，WS 断了也照写新鲜，“进程活着”不等于“连接通着”。
2. 所以修法是三件，不是搬家本身：心跳改写连接状态；90 秒 API 轮询兜底捞漏；连不上就退出、让 systemd 拉起。搬家只是把这三件放到一个不会被沙箱重启杀掉的地方。
3. 处理仍在沙箱这边（Agent 在沙箱里），服务器只负责收发。两边没有直连通道（沙箱接不住推送、SSH 走不通），所以中间加一段 Cloudflare Worker 做被动中转，两边都只走 443 出站轮询。

**架构（全程只走 443 出站，不开任何入站端口）：**

```
用户飞书 --WSS长连接--> 服务器 daemon (systemd)
    服务器 daemon --写本地 queue/inbox--> 服务器 relay-sync --POST inbox--> CF Worker (KV 按桥分键)
    沙箱 relay-sync --GET inbox--> Worker，写本地 queue/inbox
    沙箱 inbox 钩子唤醒 Agent 处理，写本地 queue/outbox
    沙箱 relay-sync --POST outbox--> Worker
    服务器 relay-sync --GET outbox--> Worker，写本地 queue/outbox
    服务器 daemon --飞书 API--> 发回用户飞书
```

- 飞书那头：服务器 daemon 直连 `open.feishu.cn` WSS，出站 443。
- 服务器和沙箱之间：**不是长连接，是轮询**。Worker 是被动 dumb 中转站，只存取、不连飞书、不处理消息。没有飞书凭证经过它，只有排队的消息 JSON。
- 同一个飞书应用只能挂一个 daemon。挂两个会把事件分流，一人一半，谁都收不全。

**关键数字（当前档）：**

| 项目 | 数值 |
|---|---|
| 同步轮询（两边 relay-sync、收信钩子） | 30 秒一轮（2026-11-25 由 15 秒改成 30 秒，用户定：用得不多，慢点没事，不断联就行） |
| daemon 轮询兜底 | 90 秒一轮，拉已知会话近 15 分钟的消息 |
| 旧消息跳过 | 超过 15 分钟（900 秒）的消息不入队 |
| WS 假死退出 | 连接曾建立、之后持续断开超 300 秒，daemon 自己退出（码 3），systemd 拉起 |
| 轮询连续失败退出 | 连续 3 次失败，daemon 自己退出重启 |
| 心跳检查 | 服务器每分钟查一次，超 300 秒未更新就 restart 服务 |
| 沙箱同步看护 | 每 60 秒查一次进程，死了自己拉起 |
| 实测往返 | 约 1～2 分钟（30 秒档；15 秒档实测约 1 分半） |
| Worker 请求量 | 30 秒档两边合计约 5760 次/天，免费额度 10 万次/天，远没到线 |
| 服务器资源 | daemon 几十 MB 内存，CPU 忽略不计 |

---

## 二、前置条件

| 需要 | 说明 |
|---|---|
| 飞书应用 | 应用所有者在飞书开放平台建企业自建应用（见第三步），拿到 App ID（`cli_` 开头）与 App Secret。**这是所有者的动作，不要替他建。** |
| 一台 Linux 服务器 | 有 root SSH。部署只新增目录与服务，不动现有内容（见第六步零影响清单）；换一台服务器做法一样。 |
| 到服务器的 SSH 路 | 按实际环境走（直连或经一台可达的中转机），以能稳定连上为准。 |
| 一个 Cloudflare 账号 | 用来建中转 Worker 与 KV。新建独立 Worker 与 KV，账号下已有的其他 Worker 不动。 |
| Agent 沙箱 | 处理消息的一端。沙箱出站只允许 443，这是走 Worker 中转而不是直连的原因。 |
| 代码 | 本流程的桥代码（见第四步）。模板在 skill `local-feishu-bridge` 的 `assets/template/`；部署包在 `~/workspace/feishu-bridge-server/`。 |

---

## 三、第一步：飞书后台建应用（应用所有者做）

1. 飞书开放平台建企业自建应用，拿到 **App ID**（`cli_` 开头，可明文）与 **App Secret**（秘密，只出现一次，建好立刻保存）。
2. 机器人能力：开启机器人，私聊可用。
3. 事件订阅：订阅 `im.message.receive_v1`（收到私聊消息的长连接事件）。
4. 权限（按后台要求开通并发布新版本）：
   - `im:message.p2p_msg:readonly`（读私聊消息）
   - `im:message:send_as_bot`（以机器人身份发消息）
   - `im:resource:upload`（或 `im:resource`，出站发附件）
   - `im:message:readonly`（入站附件下载走消息资源接口时可能需要；若资源接口报缺权限再补）
   - 群聊另加 `im:message.group_at_msg:readonly`（只有用户要求群聊时才做）
5. 发布新版本。权限改动后必须重新发布才生效。
6. 把 App ID 与 App Secret 交给部署方。**Secret 只复制一次并立刻校验，不要从聊天记录回捞，不要凭记忆转写。**

**凭证模式（动手前向应用所有者问清并记录，得到明确选择后再继续）：**

- `memory_only`（优先推荐）：Secret 只经 stdin 进入 supervisor 内存，子进程自动复活不需补给；supervisor 或整机重启后必须由所有者补发再启动。
- `local_file`（仅在所有者明确接受、机器人低风险时）：真实 `credentials.json` 放在运行目录，权限 0600，进 `.gitignore`，供 supervisor 和整机恢复自读。
- 服务器部署走本流程的 `local_file` 形态：凭证放服务器 `/etc/feishu-bridge/<桥名>/credentials.json`（0600，属主为桥的专用用户）。

---

## 四、第二步：准备代码

桥代码分两部分：**daemon（收发信）** 与 **中转（relay-sync + Worker）**。部署时直接复制现网代码，**不打补丁**。

### 4.1 daemon 代码（`bridge/`）

| 文件 | 作用 |
|---|---|
| `bridge_daemon.py` | 收消息（WS 长连接 + 90 秒 API 轮询兜底）、下载入站附件、发消息/附件、写心跳、假死自退 |
| `supervisor.py` | 启 daemon 子进程；子进程退出则退避重启；Secret 只放内存，经管道喂给子进程 |
| `file_queue.py` | 本地文件队列（inbox/outbox/processed），按 `message_id` 去重 |
| `feishu_listener.py` / `feishu_sender.py` | 飞书收/发封装 |
| `secret_provider.py` | 凭证来源（stdin 或 `--credentials-file`） |
| `start_bridge.sh` | 幂等启动脚本 |
| `validate_credentials.py` | 只输出脱敏校验结果（code/expire），不输出 Secret |
| `backfill_message.py` | 补取已送达消息的入站附件（只证明下载链可用，不替代真实新入站验收） |

**daemon 的防假死设计（必须有，缺一个都不行）：**

- 心跳写连接状态，不只写进程存活：`heartbeat.json` 含 `ws_conn_present`、`ws_last_event_at`、`ws_last_reconnected_at`、`ws_reconnect_count`、`poll_last_ok_at`、`poll_last_error`。看护按连接死活判，不按进程死活判。
- WS 与轮询共用 `process_incoming_message`：同一 `message_id` 只入队一次；轮询恢复的消息标 `source=poll`，不冒充 WS 活动。
- 每 90 秒用 `GET /im/v1/messages?container_id_type=chat&container_id=<chat_id>&page_size=N&sort_type=ByCreateTimeDesc` 拉已知会话近 15 分钟的消息；机器人自己发的消息（`sender_type=app`）跳过；超过 15 分钟的旧消息跳过。
- **去重凭 `message_id` 持久化，claim 在入队文件落盘之后才记**，崩溃在收信和入队之间不会丢信。反过来，入队前必须先查已处理过的 `message_id`，否则轮询会在 900 秒窗口内把同一条消息反复拉回来、回复被推多遍（2026-11-25 上线当天查出的毛病：同一条消息 13:20–13:35 间被拉取 7 次、回复至少被推了四次）。
- 长连接断了、SDK 300 秒没重连回来，daemon 自己退出（码 3），由 supervisor/systemd 拉起；轮询连续 3 次失败也退出重启；没消息的安静时段不触发。
- 多实例：一份代码，`FEISHU_BRIDGE_HOME` 指向每个实例自己的数据目录（配置、队列、状态各一份）；不设就是单目录老行为。
- **代码与数据分离**：代码永远走 `CODE_DIR`（supervisor.py 文件旁边），数据走 `BASE_DIR`（`FEISHU_BRIDGE_HOME`，不设时等于代码目录）。不要把两者混用，否则服务器上代码路径会被算到数据目录下，daemon 起不来。

### 4.2 中转代码

| 文件 | 作用 |
|---|---|
| `relay-sync.py` | 两边同步脚本，标准库 only。`--role sandbox` 拉收件、推回复；`--role server` 推收件、拉回复；先落盘后 ACK，推成功才移 processed，掉线不丢消息 |
| `relay-worker.js` | 中转 Worker（见第五步） |

**中转只有一条路**：daemon 只读写本地队列文件，收发信与 Worker 的同步全由 `relay-sync.py` 做。不要再给 daemon 加直连 Worker 的代码，两条路并存会把同一条回复发两遍。

### 4.3 依赖与版本坑

- Python 3.12，`pip install lark-oapi`。**版本差异是实测踩过的坑**：
  - 沙箱 venv 是 lark-oapi **1.7.3**，`_ws_connect_kwargs()` 是 0 参。
  - 服务器现装是 lark-oapi **1.9.0**，`_ws_connect_kwargs(connection_headers=None)` 是 1 参，调用时传连接头。
  - 旧代码里 `lambda: {"proxy": True}` 直接替换这个函数，在 1.9.0 上会报 `takes 0 positional arguments but 1 was given`，WS 连不上。**修法是包住原函数再改 proxy**，不要直接替换（见第十一步）。
- `requirements.txt` 只含 `lark-oapi`（其余标准库）。装完确认 `import lark_oapi` 成功、`py_compile` 通过。
- **Python 默认 UA 会被 Cloudflare 1010 拦**：`urllib` 默认发 `Python-urllib/x.y`，workers.dev 直接回 403 error code 1010。`relay-sync.py` 与任何调 Worker 的测试脚本都必须发浏览器 UA，例如 Chrome 131（见第十一步）。

---

## 五、第三步：部署 Cloudflare Worker 中转

### 5.1 建 KV 与 Worker

1. 在 Cloudflare 账号里新建 KV namespace，名字 `BRIDGE_RELAY`，记下 namespace id。
2. 新建 Worker，名字 `feishu-bridge-relay`，代码用部署包里的 `relay-worker.js`（ES module），绑定 KV `BRIDGE_RELAY`。
3. 设 secret：`BRIDGE_TOKENS`，值是 JSON map，桥名→token，例如 `{"main": "<relay-token>"}`。relay token 用 `openssl rand -hex 32` 现生成，**这是中转口令，不是飞书 Secret**。也可以用单个 `RELAY_TOKEN`，但多桥时按桥分 token 更干净。
4. **workers.dev 路由默认是关闭的**。API 上传的新 Worker 不会自动开路由，地址会回 404 error code 1042（意思是“这个地址没有 Worker 在服务”）。**必须去控制台打开**：Workers & Pages → 打开 `feishu-bridge-relay` → Domains 页 → Production Worker URL 的开关打开，刷新确认仍为开启。API 开路由的两个端点都走不通：`PATCH /workers/scripts/{name}/settings` 带 `{"workers_dev": true}` 被接受但无效果；`PUT /workers/scripts/{name}/subdomain` 带 `{"enabled": true}` 用 Global API Key 会报 code 10405（这个端点不接受 Global Key 鉴权）。所以这一步走控制台，不要在 API 上耗时间。

### 5.2 Worker 协议（以 `relay-worker.js` 为准）

- `GET /v1/health`（无鉴权）：回 `{"ok":true,"service":"feishu-bridge-relay"}`。
- `GET /v1/<桥名>/health`（鉴权）：回两队列长度。
- `POST /v1/<桥名>/inbox|outbox`，body `{"message_id": "...", "payload": {...}}`，Bearer token 鉴权 → `{"ok":true,"message_id":"..."}`。
- `GET /v1/<桥名>/inbox|outbox` → `{"ok":true,"messages":[{"message_id":"...","payload":{...}}]}`。
- `POST /v1/<桥名>/inbox|outbox/ack`，body `{"message_id":"..."}` → 删除该条。
- KV 键：`<kind>:<桥名>:<message_id>` 存消息，`<kind>:<桥名>:index` 存 id 列表（上限 500）。
- **先落盘后 ACK**：拉取方把消息写到本地文件后才调 ack；推的一方推成功才把本地文件移进 processed。中间任何一步断了，消息还在，不丢。
- KV 是最终一致：ack 后立刻拉可能还看到刚删的消息，等几秒再拉就没了。这是正常现象，不是 bug。

### 5.3 验 Worker（路由开后立刻做）

1. `GET /v1/health` 应回 `{"ok":true,...}`（HTTP 200）。
2. 往返测试：用 relay token 对 `/v1/<桥名>/inbox` 做 push → pull → ack → 再 pull（应为空）。**测试脚本调 Worker 必须发浏览器 UA**，否则 1010（见 4.3）。
3. 全部 200 才算 Worker 通了，再往下走。

---

## 六、第四步：部署服务器

### 6.1 零影响清单（部署时逐项核对）

| 现有东西 | 部署动作 | 影响 |
|---|---|---|
| 服务器现有服务与入站端口 | 不碰 | 无 |
| 443（已不再对外监听） | 只多一路 443 **出站**，源端口是系统随机临时端口，不占 443 | 无 |
| 22 / cloudflared 隧道 | 不碰、不重启 | 无 |
| 账号下已有的其他 Worker | 新建独立 Worker + 新 KV，旧的不动 | 无 |
| 服务器现有文件 | 只新增 `/opt/feishu-bridge`（代码）、`/var/lib/feishu-bridge/<桥名>`（数据）、`/etc/feishu-bridge/<桥名>/`（凭证与中转 token）、三个 systemd 单元 | 无 |

回滚＝停服务、删新增的目录与单元，服务器恢复原样。

### 6.2 传代码、建 venv

1. 把 `bridge/`（已含防假死改动，`py_compile` 通过）与 `relay-sync.py` 拷到服务器 `/opt/feishu-bridge/`。**传包一次打完**：改完了一次打一个包再传，不要零敲碎打分几次传，每次传都要应用所有者点一次确认卡。
2. 服务器上建 venv 并装依赖：
   ```bash
   python3 -m venv /opt/feishu-bridge/.venv
   /opt/feishu-bridge/.venv/bin/pip install lark-oapi
   /opt/feishu-bridge/.venv/bin/python -c "import lark_oapi; print('lark-oapi OK')"
   ```
   注意服务器装到的 lark-oapi 版本（见 4.3 的 1.7.3/1.9.0 差异），代码里的 `_ws_connect_kwargs` 包装必须兼容两个版本。
3. **权限**：代码目录默认 root 拥有，daemon 跑在专用用户下，读不到会报 `Permission denied`。部署后把代码目录统一放开可读可执行，例如 `chmod -R a+rX /opt/feishu-bridge`。重点是 `bridge/` 目录本身要有执行权限（能进入），否则 supervisor 起不来。
4. 建专用系统用户（只建一次）：
   ```bash
   useradd --system --home-dir /var/lib/feishu-bridge --shell /usr/sbin/nologin feishu-bridge
   ```

### 6.3 放配置、凭证、中转 token

| 文件 | 位置 | 权限 | 内容 |
|---|---|---|---|
| `config.json` | `/var/lib/feishu-bridge/<桥名>/config.json` | 644，属主桥用户 | 见下 |
| `credentials.json` | `/etc/feishu-bridge/<桥名>/credentials.json` | **0600**，属主桥用户 | `{"app_id": "cli_...", "app_secret": "..."}` |
| `relay-token` | `/etc/feishu-bridge/<桥名>/relay-token` | **0600**，属主桥用户 | 一行，中转口令（与 Worker 的 `BRIDGE_TOKENS` 里对应桥的值一致） |
| 数据目录 | `/var/lib/feishu-bridge/<桥名>/` 下 `queue/`、`state/`、`logs/` | 属主桥用户 | daemon 与 relay-sync 读写 |

`config.json` 关键字段（以实测的 `main` 桥为例）：

```json
{
  "app_id": "cli_...",
  "api_host": "open.feishu.cn",
  "base": "feishu",
  "allowed_chat_ids": ["oc_..."],
  "allowed_open_ids": ["ou_..."],
  "queue_dir": "queue",
  "secret_provider": "transient",
  "require_mention_in_group": true,
  "fallback_poll_interval_seconds": 90,
  "fallback_poll_window_seconds": 900,
  "ws_stale_exit_seconds": 300,
  "poll_interval_seconds": 20
}
```

- `allowed_chat_ids` / `allowed_open_ids`：白名单。先跑起来、从首条真实消息把用户的 `open_id` 与已验证的 `chat_id` 回填进去，再验一条。
- `secret_provider: transient`：Secret 只经 stdin 进 supervisor 内存，不落盘在代码目录（服务器上凭证文件由 supervisor 的 `--credentials-file` 读，读完只放内存）。
- 凭证传输：不要把 Secret 贴进聊天。做法是把 `credentials.json` 传到一个临时链接（设过期时间，例如 2 天），服务器那头直接从链接取下放到 `/etc/feishu-bridge/<桥名>/credentials.json`，改 0600、改属主，验证 JSON 能解析、`app_id` 对得上、`app_secret` 非空（不打印值），然后删掉临时文件。临时链接过期即废。

### 6.4 装 systemd 服务并启动

三个单元（都在部署包里，`%i` 是桥名）：

**`feishu-bridge@.service`**（daemon，supervisor 拉起 daemon 子进程）：

```ini
[Unit]
Description=Feishu bridge %i (one Feishu app per instance)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=feishu-bridge
Group=feishu-bridge
Environment=FEISHU_BRIDGE_HOME=/var/lib/feishu-bridge/%i
ExecStart=/opt/feishu-bridge/.venv/bin/python -u /opt/feishu-bridge/bridge/supervisor.py --credentials-file /etc/feishu-bridge/%i/credentials.json
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

**`feishu-relay-sync@.service`**（中转同步，30 秒一轮）：

```ini
[Unit]
Description=Feishu bridge relay sync %i (Worker <-> local queue)
After=network-online.target feishu-bridge@%i.service
Wants=network-online.target

[Service]
Type=simple
User=feishu-bridge
Group=feishu-bridge
ExecStart=/opt/feishu-bridge/.venv/bin/python -u /opt/feishu-bridge/relay-sync.py --role server --bridge %i --url https://feishu-bridge-relay.<子域名>.workers.dev --home /var/lib/feishu-bridge/%i --token-file /etc/feishu-bridge/%i/relay-token --interval 30
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

**`feishu-heartbeat-check.service` + `feishu-heartbeat-check.timer`**（心跳看护，每分钟）：

```ini
# feishu-heartbeat-check.service
[Unit]
Description=Feishu bridge heartbeat check (restart if stale)
[Service]
Type=oneshot
ExecStart=/opt/feishu-bridge/feishu-heartbeat-check.sh
```

```ini
# feishu-heartbeat-check.timer
[Unit]
Description=Check Feishu bridge heartbeat every minute
[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
Persistent=true
[Install]
WantedBy=timers.target
```

心跳检查脚本（`/opt/feishu-bridge/feishu-heartbeat-check.sh`，755）：读 `/var/lib/feishu-bridge/<桥名>/state/heartbeat.json` 的 `ts`，超过 300 秒未更新就 `systemctl restart feishu-bridge@<桥名>`，只写 journal，不联系 Agent。

启动与验证：

```bash
systemd-analyze verify /etc/systemd/system/feishu-bridge@.service /etc/systemd/system/feishu-relay-sync@.service /etc/systemd/system/feishu-heartbeat-check.service /etc/systemd/system/feishu-heartbeat-check.timer
systemctl daemon-reload
systemctl enable --now feishu-bridge@main
systemctl enable --now feishu-relay-sync@main
systemctl enable --now feishu-heartbeat-check.timer
systemctl is-active feishu-bridge@main feishu-relay-sync@main
cat /var/lib/feishu-bridge/main/state/heartbeat.json
journalctl -u feishu-bridge@main --no-pager -n 20
```

**必须看到的证据**（缺一个都不算成）：

- `systemctl is-active` 两个服务都是 `active`。
- `heartbeat.json` 里 `ws_conn_present: true`（WS 已连上），`poll_last_ok_at` 在更新（轮询兜底在跑）。
- journal 里有 `[Lark] [INFO] connected to wss://msg-frontier.feishu.cn/ws/v2?...`（只报“已连上”，不摘抄地址和 ticket）。
- supervisor 日志 `supervisor started; secret held only in memory` 与 `daemon child started`。

---

## 七、第五步：配置沙箱侧

沙箱这边不跑 daemon（**必须保持停**，同一时间只跑一套桥），只跑两样：同步 + 处理。

### 7.1 起沙箱 relay-sync

```bash
nohup python3 ~/workspace/feishu-bridge-server/relay-sync.py \
  --role sandbox --bridge main \
  --url https://feishu-bridge-relay.<子域名>.workers.dev \
  --home ~/workspace/feishu-bridge \
  --token-file ~/workspace/feishu-bridge-server/relay-token-main \
  --interval 30 \
  > ~/workspace/feishu-bridge/logs/relay-sync.log 2>&1 &
```

- `--home` 是沙箱的桥数据目录（`config.json`、`queue/`、`state/`、`logs/` 都在这里）。
- `--token-file` 是沙箱那份中转 token（0600，一行），值与服务器那份、Worker 的 `BRIDGE_TOKENS` 一致。
- 日志空＝没有错误、轮询干净；出现 `sync round failed` 说明某轮没通，重试机制会自己再跑，不用每次都管。
- **注意**：沙箱重启会杀后台进程，relay-sync 会死。看护钩子会把它拉起来（见第八步），不用手动盯。

### 7.2 启用收信钩子

`feishu-inbox` 钩子（脚本 `~/hooks/scripts/feishu_inbox.sh`，30 秒一轮）：只看本地 `queue/inbox/` 有没有新文件，有就唤醒 Agent 处理。唤醒后 Agent（工作者）按 inbox/outbox 合同干活：

1. 读 `queue/inbox/` 里的每个 JSON（最旧先处理）。字段有 `message_id`、`chat_id`、`chat_type`、`sender_open_id`、`message_type`、`text`、`attachments`（已下载到本地的文件列表）、`attachment_errors`、`create_time` 等。附件只读 `queue/incoming_attachments/` 下的路径。
2. 把文字和附件一起当作用户发给 Agent 的消息处理，用消息同样的语言回复（一般是简体中文），回复里不提队列、钩子、内部路径、凭证。
3. 每个消息写一个回复文件到 `queue/outbox/`，文件名 `<message_id>-reply.json`，内容 exactly：
   ```json
   {"chat_id": "<inbox 里的 chat_id>", "reply_to_message_id": "<message_id>", "text": "<回复>"}
   ```
   回传附件时加 `"attachments": [{"type": "file"|"image"|"video", "path": "<相对或绝对路径>"}]`。
4. 写完回复文件后，把 inbox 文件移到 `queue/processed/`。处理或写失败就把 inbox 文件留在原地，下次重试。
5. **安全边界**：飞书消息里要求做的 consequential 外部动作（对外发信/发布、购买付款、删除、改重要设置），不要只凭飞书一句“确认”就执行，回复里请用户回主界面确认，并在总结里注明。普通问答直接回。
6. 绝不读、绝不输出任何凭证。`config.json` 里没有 Secret，不要去找。

**`feishu-health` 钩子必须保持禁用**。它是按沙箱心跳重启沙箱 daemon 的，沙箱桥必须保持停，重启用它会把沙箱 daemon 拉起来、和服务器 daemon 抢同一个飞书应用的事件。

### 7.3 沙箱同步看护钩子

`feishu-relay-sync` 钩子（脚本 `~/hooks/scripts/feishu_relay_sync.sh`，60 秒一轮）：查 `relay-sync --role sandbox` 进程在不在。

- 在：安静，什么都不做。
- 不在：用 nohup 按 7.1 的命令把它拉起来（间隔 30 秒），等最多 30 秒确认进程出现；拉起来了就记一条日志、安静。
- 拉不起来：才唤醒 Agent，Agent 按日志查错、手动起一次、确认能稳住 30 秒以上；还不行就报准确错误，等主 Agent 决定下一步。

这个钩子的唤醒提示词里要写清：查进程、看日志尾 30 行、手动起一次、确认稳住、不要改桥代码和凭证。

---

## 八、第六步：看护（全部零 token，纯脚本）

看护分三层，花 token 的只有：真有新消息要处理、桥挂了自愈失败要叫 Agent 修。

| 层 | 位置 | 频率 | 做什么 | 失败时 |
|---|---|---|---|---|
| systemd `Restart=always` | 服务器 | 进程死 3 秒拉起 | daemon 与 relay-sync 进程死了就拉起，开机自启 | 连续崩 5 次/10 秒会停手，等下一层 |
| 心跳检查 timer | 服务器 | 每分钟 | 心跳超 300 秒未更新就 `systemctl restart`，只写 journal | 只重启、不通知；一直起不来要靠用户发现（发消息没回）或定期查 journal |
| relay-sync 看护钩子 | 沙箱 | 每 60 秒 | relay-sync 进程死了就拉起 | 拉不起来才唤醒 Agent |
| 收信钩子 | 沙箱 | 每 30 秒 | inbox 有新文件就唤醒 Agent 处理 | 队列本身不丢消息，晚点处理而已 |

**daemon 自己的两层自愈（不用外部看护）：** WS 断 300 秒自己退出（码 3）由 systemd 拉起；轮询连续 3 次失败也自己退出重启。安静时段（没消息）不触发，不会误判。

**看护不烧 token 的原因**：重启、检查、拉起全是脚本（systemd、bash、钩子脚本）。Agent 只在两种情况出现：有新消息要处理；自愈失败要修。

---

## 九、第七步：端到端验收（必须真发消息验）

没有实际证据不得打勾，也不得跳关。按顺序验，每段都要对上时间：

1. 用户在飞书私聊机器人发一条约定测试语（例如「测试123」）。
2. 服务器 daemon 日志出现 `inbox enqueued message_id=<id> ... source=ws`（WS 收到）或 `source=poll`（轮询捞到）。
3. 服务器 relay-sync 日志出现 `pushed inbox message_id=<id>`（已推上 Worker）。
4. 沙箱 relay-sync 日志出现 `pulled inbox message_id=<id>`（已拉回沙箱）。
5. 收信钩子唤醒 Agent，Agent 写 `queue/outbox/<id>-reply.json`，把 inbox 文件移进 `processed/`。
6. 沙箱 relay-sync 日志出现 `pushed outbox message_id=<id>-reply`。
7. 服务器 relay-sync 日志出现 `pulled outbox message_id=<id>-reply`。
8. 服务器 daemon 日志出现 `outbox sent ... file=<id>-reply.json`。
9. **用户飞书端亲眼确认收到回复。** 前面 8 步全是日志证据，只有这一步是真正的验收通过。

**实测基线**：2026-11-25，30 秒档，全程约 1～2 分钟。15 秒档实测约 1 分半（其中一次沙箱到 Cloudflare 的连接断了两次、重试才过去）。报回复时间按实测说，不要按理论值说。

**验收通过的标准**：第 9 步用户亲眼确认。只看日志、只看 API code 0，都不算通过。

**附件验收**（文字通了之后再验）：出站先生成 txt/png/mp4 三个小样逐个发，用户确认能打开图片、播放视频；入站请用户新发一张图片和一个小视频，daemon 日志应显示对应 `type` 与 `attachments` 数量且 `attachment_errors=0`，inbox JSON 带本地附件路径，文件真实落在 `queue/incoming_attachments/<message_id>/`，工作者能读到并把回复发回。`backfill_message.py` 补到旧消息只证明下载链可用，不能替代真实新入站事件验收。

---

## 十、正常使用

### 10.1 消息怎么走（一次完整往返）

用户飞书发消息 → 服务器 daemon（WS 即时收，或 90 秒轮询捞到）→ 写服务器本地 `queue/inbox/` → 服务器 relay-sync（30 秒一轮）推上 Worker inbox → 沙箱 relay-sync（30 秒一轮）拉回沙箱 `queue/inbox/` → 收信钩子（30 秒一轮）唤醒 Agent → Agent 处理、写沙箱 `queue/outbox/<id>-reply.json` → 沙箱 relay-sync 推上 Worker outbox → 服务器 relay-sync 拉回服务器 `queue/outbox/` → 服务器 daemon 调飞书 API 发回用户飞书。

### 10.2 用户侧看到的样子

- 在飞书私聊里像跟一个助手聊天：发文字、图片、视频、文件都能回。
- 回复延迟约 1～2 分钟（30 秒档），不是即时。WS 收信是即时的，慢在两边轮询。
- 飞书侧只做轻量工作：文字聊天、发素材供 Agent 分析、约定的轻量编辑、回传成品附件。
- **飞书侧不做的事**：浏览器画面、浏览器接管、主界面的结构化审批、安全录入卡、钱包确认、付款、对外发信/发布、重要删除、高价值账号设置。这些必须回主界面做，不以飞书一句“确认”替代。

### 10.3 Agent 侧的日常

- 平时什么都不用做。消息来了钩子会唤醒，处理完写回复文件，同步自己会推回去。
- 定期看一眼就行：服务器两个服务 active、心跳新鲜、沙箱 relay-sync 进程在、relay-sync 日志没有持续报错。
- 断线期间飞书发来的消息找不回来（长连接断线期间飞书不补发），重要的事请用户到主聊天重发。

### 10.4 队列目录结构（两边一样）

```
<数据目录>/
  config.json
  queue/
    inbox/        # 待处理的消息（daemon/relay-sync 写，Agent 读）
    outbox/       # 待发送的回复（Agent 写，daemon/relay-sync 读）
    processed/    # 已处理/已发送，归档
    incoming_attachments/<message_id>/  # 入站附件
  state/
    heartbeat.json          # daemon 心跳（含 ws_conn_present 等）
    supervisor_heartbeat.json
    daemon_child.json
    seen_message_ids.json   # 去重：已处理过的 message_id
    known_chat_ids.json
    last_sender.json
    last_send_error.json    # 先看修改时间再下结论
  logs/
    daemon.log
    supervisor.log
    relay-sync.log
```

---

## 十一、多桥部署

- 一个 Worker，按桥分键。`BRIDGE_TOKENS` 里每桥一个 token。
- 每个桥是不同的飞书机器人，各自独立：daemon、凭证、队列、数据目录、systemd 服务（`feishu-bridge@<桥名>.service`、`feishu-relay-sync@<桥名>.service`）。
- 新桥要先在飞书后台建新应用、拿新凭证（凭证模式按第三步由应用所有者选），再按第六步在服务器上建对应的数据目录、凭证文件、服务；沙箱侧起对应桥的 relay-sync（`--bridge <桥名>`），钩子按桥配。
- **铁律不变：同一个飞书应用只能有一个消费者。** 多桥指多个不同的飞书机器人，不是同一个应用挂两个 daemon。

---

## 十二、常见问题（排错表）

| 现象 | 原因 | 修法 |
|---|---|---|
| Worker 地址 404，error code 1042 | 新 Worker 的 workers.dev 路由默认关闭 | 去控制台 Domains 页把 Production Worker URL 开关打开，刷新确认仍为开启。API 开路由走不通，不要耗时间 |
| 调 Worker 回 403，error code 1010 | Python 默认 UA 被 Cloudflare 拦 | 所有调 Worker 的代码与测试脚本发浏览器 UA（Chrome 131）。curl 默认 UA 不会被拦 |
| 调 CF API 报 Invalid API Token，code 1000 | **验法错了**：`/user/tokens/verify` 只认 API Token，不认 Global API Key | `cfk_` 开头的 Global API Key 一律用 `X-Auth-Email` + `X-Auth-Key` 请求头验。报“无效”之前先确认验法对不对 |
| daemon 起不来，`Permission denied` | 代码目录权限：`bridge/` 目录是 750，桥用户进不去 | `chmod -R a+rX /opt/feishu-bridge`，重点是目录要有执行权限 |
| daemon 起不来，`child spawn failed error=FileNotFoundError` | supervisor 把代码路径算到数据目录下（`FEISHU_BRIDGE_HOME` 拆分后） | 代码走 `CODE_DIR`（supervisor.py 文件旁边），数据走 `BASE_DIR`（`FEISHU_BRIDGE_HOME`）。不要把两者混用 |
| WS 连不上，`lambda() takes 0 positional arguments but 1 was given` | lark-oapi 版本差异：1.7.3 的 `_ws_connect_kwargs()` 是 0 参，1.9.0 是 1 参（带 connection_headers） | **包住原函数再改 proxy**，不要直接替换：`_orig = _ws_mod._ws_connect_kwargs`，再定义一个 `*args, **kwargs` 的包装调原函数、改 `proxy=True`。这样两个版本都通，且连接头不会丢 |
| 同一条消息反复收到、回复被推多遍 | 入队前没查已处理过的 `message_id`；轮询在 900 秒窗口内把同一条反复拉回来 | 入队前先查 `seen_message_ids.json`（claim 在入队落盘之后才记）；服务器 daemon 与沙箱轮询侧两边都要覆盖。修完拿日志验一遍：同一 `message_id` 只入队一次、只推一次 |
| relay-sync 日志 `sync round failed error=RemoteDisconnected` | 沙箱到 Cloudflare 的连接被重置（沙箱出口代理不稳） | 不用手动处理，重试机制下一轮会自己再跑。连续多轮都失败再查 |
| ack 后立刻拉还看到刚删的消息 | KV 最终一致，删除还没扩散到所有节点 | 等几秒再拉。不是 bug |
| `SSL: WRONG_VERSION_NUMBER` | 走了直连，没走代理（沙箱出站要走环境代理） | 分开查 REST 与 WS 及代理发现；WS 连接要开代理发现（见 4.3） |
| 飞书报 99991672 | 缺权限 | 补后台权限并发布新版本 |
| 飞书报 10014 / 1000040345 | 凭证无效 | 停重试，请所有者单独一行重发 Secret，不要边猜边重启 |
| 发 MP4 报 230055 | MP4 错用了 file 消息 | 改 media |
| 用户发了视频却毫无反应，日志有 `inbox ignored empty/non-text` | 跑的是旧版纯文字入站 daemon | 升级 daemon，不是让用户重发 |
| 心跳新鲜但 WS 早断了 | 心跳只写了进程存活，没写连接状态 | 心跳必须含 `ws_conn_present` 等连接字段（见 4.1），看护按连接死活判 |

---

## 十三、停止与回滚

**停桥（临时）：**

```bash
# 服务器
systemctl stop feishu-bridge@main feishu-relay-sync@main
systemctl disable feishu-bridge@main feishu-relay-sync@main
# 沙箱
pkill -f 'relay-sync[.]py --role sandbox'   # 用脚本文件跑，不要直接写命令行（会自匹配）
# 钩子
hooks.disable feishu-inbox
hooks.disable feishu-relay-sync
# feishu-health 本来就是禁用的，保持禁用
```

**回滚（彻底拆掉，服务器恢复原样）：**

```bash
systemctl stop feishu-bridge@main feishu-relay-sync@main feishu-heartbeat-check.timer
systemctl disable feishu-bridge@main feishu-relay-sync@main feishu-heartbeat-check.timer
rm /etc/systemd/system/feishu-bridge@.service /etc/systemd/system/feishu-relay-sync@.service \
   /etc/systemd/system/feishu-heartbeat-check.service /etc/systemd/system/feishu-heartbeat-check.timer
systemctl daemon-reload
rm -rf /opt/feishu-bridge /var/lib/feishu-bridge /etc/feishu-bridge
userdel feishu-bridge
```

Worker 与 KV 在 Cloudflare 控制台删（`feishu-bridge-relay` 与 `BRIDGE_RELAY`）。沙箱侧删 `~/workspace/feishu-bridge-server/` 与钩子脚本前，先确认没有未处理的消息在队列里。

**删不掉的部分**：飞书应用本身（要去飞书后台删）、Cloudflare 账号、已发出的消息、用户手里已下载的文件。

---

## 十四、关键教训（都是实测踩出来的）

1. **心跳必须写连接状态，不只写进程存活。** “进程活着”不等于“能用”。没跑通一次真实收发就不说验通过了。
2. **沙箱不适合跑 24 小时监听。** 沙箱替换专杀后台进程，自愈只在 supervisor 活着时才管用。长期监听放服务器。
3. **验凭证先确认验法对不对，再下“无效”结论。** Global API Key 用 `X-Auth-Email` + `X-Auth-Key` 验，绝不用 `/user/tokens/verify`。
4. **workers.dev 路由默认关闭，API 开不通，走控制台。** 404 code 1042 就是路由没开。
5. **Python 默认 UA 会被 Cloudflare 1010 拦。** 调 Worker 的代码一律发浏览器 UA。
6. **SDK 版本差异要包住原函数，不要直接替换。** lark-oapi 1.7.3 与 1.9.0 的 `_ws_connect_kwargs` 签名不一样。
7. **代码与数据分离，CODE_DIR 与 BASE_DIR 不要混用。**
8. **去重凭 `message_id`，claim 在入队落盘之后才记；入队前先查已处理过的 id。** 两边都要覆盖，否则轮询会把同一条反复拉回来。
9. **传包一次打完。** 改完了一次打一个包再传，不要零敲碎打分几次传，每次传都要应用所有者点一次确认卡。
10. **中转只有一条路。** daemon 只读写本地队列文件，同步只走 `relay-sync.py`。两条路并存会把同一条回复发两遍。
11. **部署前先审计。** 检查目标目录是否已有另一套桥、是否有 supervisor/daemon 在跑、钩子是否已注册、队列是否有未处理消息。同一机器人只能有一套消费者。
12. **凭证只复制一次并立刻校验。** 不要从聊天记录回捞，不要凭记忆转写，不要边猜边重启。Secret 绝不写进代码、日志、记忆、命令参数、systemd unit。
13. **验收按证据关口逐个关。** 用户飞书端亲眼确认收到回复，才算通过。日志与 API code 0 都只是中间证据。

---

## 附：文件清单（本流程涉及的全部文件）

| 文件 | 位置 | 说明 |
|---|---|---|
| 桥代码 `bridge/` | 服务器 `/opt/feishu-bridge/bridge/`；沙箱 `~/workspace/feishu-bridge/bridge/` | daemon、supervisor、队列、收发封装 |
| `relay-sync.py` | 服务器 `/opt/feishu-bridge/`；沙箱 `~/workspace/feishu-bridge-server/` | 两边同步脚本 |
| `relay-worker.js` | Cloudflare Worker；沙箱 `~/workspace/feishu-bridge-server/` | 中转 Worker |
| `feishu-bridge@.service` | 服务器 `/etc/systemd/system/`；沙箱 `~/workspace/feishu-bridge-server/` | daemon 服务模板 |
| `feishu-relay-sync@.service` | 同上 | 同步服务模板 |
| `feishu-heartbeat-check.sh` / `.service` / `.timer` | 服务器 `/opt/feishu-bridge/` 与 `/etc/systemd/system/`；沙箱 `~/workspace/feishu-bridge-server/` | 心跳看护 |
| `config.json` | 服务器 `/var/lib/feishu-bridge/<桥名>/`；沙箱 `~/workspace/feishu-bridge/` | 桥配置 |
| `credentials.json`（0600） | 服务器 `/etc/feishu-bridge/<桥名>/`；沙箱 `~/workspace/feishu-bridge/` | 飞书 App ID + Secret |
| `relay-token` / `relay-token-main`（0600） | 服务器 `/etc/feishu-bridge/<桥名>/`；沙箱 `~/workspace/feishu-bridge-server/` | 中转口令 |
| `feishu_inbox.sh` | 沙箱 `~/hooks/scripts/` | 收信钩子（30 秒） |
| `feishu_relay_sync.sh` | 沙箱 `~/hooks/scripts/` | 同步看护钩子（60 秒） |
| `feishu_health.sh` | 沙箱 `~/hooks/scripts/` | 健康钩子（**禁用**，沙箱 daemon 保持停） |
| skill | `~/workspace/custom-skills/local-feishu-bridge/` | 模板、CHECKLIST、references/full-guide.md |
| 部署包 | `~/workspace/feishu-bridge-server/` | DEPLOY.md、relay-worker.js、relay-sync.py、systemd 模板、本流程文档 |
