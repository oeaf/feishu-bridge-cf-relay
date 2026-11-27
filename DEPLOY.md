# 飞书桥自部署流程（agent 施工单）

> 这份文件是给**部署这座桥的 agent 本人**看的。你将独立完成全部部署：飞书应用、服务器、Cloudflare 中转、你自己这侧的同步与钩子。没有中间人替你传话——需要账号/资源时，按文中标记**直接向用户索取**。
>
> 架构与原理见 [README.md](README.md)（完整指南）。本文件只讲施工顺序、隔离纪律和验收。

## 零、隔离铁律（先读，违反任何一条立即停工报告用户）

1. **先给自己定一个 slug**：全小写字母/数字/连字符，如 `muse-7`。你的一切——服务器目录、systemd 服务名、Worker 里的 KV 键前缀、沙箱侧目录与进程——全部带这个 slug。
2. **先查后建**：创建任何东西之前，先确认同名物不存在（目录、服务、KV 命名空间、Worker 路由）。已存在且不是你的 → 停工，换 slug 或报告用户，**绝不覆盖、绝不复用别人的**。
3. **只碰自己的东西**：不许停、改、重启任何不带你 slug 的服务/进程/文件；不许占用任何已被占用的端口（本架构服务器侧零入站端口，天然无冲突——你若想加监听端口，停工问用户）。
4. **服务器上桥目录之外的系统改动**（SSH 配置、防火墙、其他服务）一律先向用户讲清「改什么、为什么、影响什么、怎么还原」并等他点头。一次只改一处，改完立刻验证。
5. **凭据纪律**：一切密钥/令牌只进部署机的本地文件（权限 600）或用户指定的保险箱；绝不写进聊天、绝不写进任何仓库、绝不出现在自检报告里。

## 一、向用户索取的资源（两个开口点，缺了就停在这里等）

按顺序要，前一个没到位不开工后一个：

1. **服务器**：一台能跑 systemd 的 Linux 服务器的 SSH 访问（地址、用户、你的公钥要被加入）。向用户说明用途：只在 `/root/workspace/feishu-bridge-<slug>/`（或用户指定根目录）下放你自己的桥，只新增带你 slug 的 systemd 单元。
2. **Cloudflare 账号**：用于建 Worker + KV 中转。优先要一个 **API Token**（权限：Account · Workers Scripts: Edit、Account · Workers KV Storage: Edit、Account · Account Settings: Read），通过用户给的安全录入方式收取，不要让用户在聊天里明文发。若账号从没启用过 Workers，可能需要用户在控制台 Workers 页面点一次启用 workers.dev 子域名——碰到再向用户说明确切位置。

飞书应用**不用向用户要**：你用 `bridge/scan_create_app.py` 扫码一键创建你自己的应用（走官方 register_app 流程），机器人名字你自己定，建完把真名记下来——后续一切身份判断以飞书接口现查为准，不凭记忆。

## 二、施工顺序（每步做完打勾再下一步）

### 1. 本地准备
- [ ] 定 slug，通读 README.md 全文
- [ ] 复制本仓库模板到你的工作区，生成你的配置：`bridge/config.json`、`bridge/credentials.json`（扫码建应用后回填）、`relay` 两侧的 sync 配置（relay_url、relay_token、slug、各队列目录）。relay_token 自己生成一串足够长的随机值，只存本地文件

### 2. 飞书应用
- [ ] 跑 `bridge/scan_create_app.py`，请用户扫码完成创建
- [ ] 跑 `bridge/validate_credentials.py` 验凭证；再调 `bot/v3/info` 现查机器人真名 + app_id，与配置逐字对齐

### 3. Cloudflare 中转
- [ ] 用 API Token 查账号、确认 Workers 已启用（未启用 → 请用户控制台点启用）
- [ ] 建 KV 命名空间（名字带 slug 或用仓库统一命名空间 + slug 前缀，二选一并记录）
- [ ] 部署 `relay/relay-worker.js`（绑定 KV 为 `RELAY_KV`，环境变量 `RELAY_TOKEN` 设为你生成的令牌；多桥共用一个 Worker 时改用 `BRIDGE_TOKENS` 按 slug 映射）
- [ ] 启用 workers.dev 路由（注意：控制台未启用时 API 建路由会报 1042，必须先在控制台启用）
- [ ] 自测：`GET /health` 通；带令牌 `GET /hb/<slug>` 返回 404（尚无心跳）即鉴权与路由都对

### 4. 服务器侧
- [ ] SSH 上服务器，**先盘点**：`systemctl list-units | grep feishu`、`ls` 目标根目录——确认你的 slug 无冲突，并看清已有哪些别人的桥（绕开清单）
- [ ] 建你的目录 `feishu-bridge-<slug>/`，放 `bridge/` 代码、配置、凭证（600），建队列目录（inbox/outbox/processed/state）
- [ ] `pip install -r bridge/requirements.txt`（lark-oapi 按 requirements 的版本说明锁定）
- [ ] 装 systemd 单元：`ops/feishu-bridge-<slug>.service`（daemon；退出码 3 已列入 SuccessExitStatus）、`ops/feishu-relay-server-<slug>.service`（relay-sync 组合模式 server）、`ops/feishu-watchdog-<slug>.timer`（看门狗：心跳陈旧或 WS 断开超 300 秒才重启 daemon，10 分钟冷却）
- [ ] 逐个启动并验活：daemon 心跳文件出现且 `ws_state=connected`；relay-sync 在跑且把心跳推上了 Worker（`GET /hb/<slug>` 有值）

### 5. 你自己这侧（沙箱）
- [ ] 建你的本地队列目录（带 slug），填 sync 配置（模式 sandbox：pull 入站 + push 出站）
- [ ] 跑 `agent/start-relay-sync.sh` 拉起常驻同步（pidfile 拒双开）
- [ ] 注册两个平台钩子（只读，不发任何请求到飞书）：取信钩子（`agent/hooks/feishu_inbox_local.sh`，间隔 30 秒，只看本地 inbox，有新文件才 wake 并带文件清单）、看护钩子（`agent/hooks/feishu_watchdog.sh`，间隔 60 秒，同步进程死了自拉起并 wake 报告，心跳陈旧超 10 分钟 wake）
- [ ] worker 指令按 `agent/hooks/prompts.md` 落定：固定工作区、读 inbox 回 outbox、禁止问用户选工作区、禁止系统腔

### 6. 验收（缺一不可）
- [ ] 跑 `selfcheck.sh`，全项 PASS（SKIP 只允许出现在与当前侧无关的项上）
- [ ] 请用户在飞书给你的机器人发约定的测试语，九段证据对齐（指南第九节）：飞书发送 → daemon 入队时间 → server-push → Worker → sandbox-pull 落盘时间 → 钩子 wake → 你回复落 outbox → sandbox-push → server-pull → daemon 发出 → 用户在飞书亲眼确认
- [ ] 把 selfcheck 报告块 + 九段时间线原样发给用户

## 三、自检报告格式（selfcheck.sh 的输出，发用户时原样贴）

```
=== FEISHU BRIDGE SELFCHECK ===
slug: <slug>
identity: PASS bot=<真名> app_id=<cli_...>
processes: PASS ...
relay: PASS ...
heartbeat: PASS age=<n>s ws_state=connected
=== END ===
```

任何一项 FAIL：先自己按指南第八节故障表排查，修不好带着报告和日志找用户，不要静默、不要假报通过。

## 四、日常维护（部署完就是你自己的事）

- 同步进程/daemon 死了有看门狗与钩子兜底；你每次被唤醒时顺手看一眼心跳年龄，异常主动报告用户
- 升级代码只升自己的目录与单元，升完重跑 selfcheck + 一条真消息验收
- 这座桥的任何事实（机器人在哪台机、真名、slug、Worker 地址）以现查为准，不凭旧记录
