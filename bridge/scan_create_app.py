#!/usr/bin/env python3
"""Scan-to-create a Feishu/Lark app and wire its credentials into this bridge.

Uses the official one-click flow (Feishu Open Platform, "Create an app in
one click"): the Python SDK's ``lark.register_app`` implements the OAuth 2.0
Device Authorization Grant (RFC 8628). The script shows a QR code / URL, the
app owner scans it with the Feishu (or Lark) mobile app and confirms, and the
SDK returns the new app's credentials directly to this process.

What this script does with the result:
- writes ``credentials.json`` (app_id + app_secret) with mode 0600 in the
  bridge runtime directory (this is the ``local_file`` credential mode);
- updates ``config.json``: app_id, base/api_host from the tenant brand, and
  appends the scanning user's open_id to ``allowed_open_ids``;
- exchanges a tenant access token once and prints only code/msg/expire.

It never prints the App Secret or any token. If ``credentials.json`` already
holds a non-empty app_id, it refuses to overwrite unless ``--force`` is given.

Test hook: when the environment variable ``SCAN_CREATE_MOCK`` points to a
JSON file shaped like the register_app result
(``{"client_id": ..., "client_secret": ..., "user_info": {...}}``), the SDK
call is skipped and that file is used instead. Never use it in production.
"""
import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
CRED_PATH = BASE_DIR / "credentials.json"
STATE_DIR = BASE_DIR / "state"
QR_PNG = STATE_DIR / "scan_qr.png"
QR_SVG = STATE_DIR / "scan_qr.svg"

# Permissions/events this bridge needs, pre-filled into the scan confirm page
# via `addons` (additive on top of the platform base template; unknown names
# are ignored by the platform, sensitive config such as encrypt keys cannot
# travel through addons).
ADDONS = {
    "scopes": {
        "tenant": [
            "im:message.p2p_msg:readonly",
            "im:message.group_at_msg:readonly",
            "im:message:send_as_bot",
            "im:resource:upload",
        ],
    },
    "events": {"items": {"tenant": ["im.message.receive_v1"]}},
    "callbacks": {"items": ["card.action.trigger"]},
}


def write_json_0600(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.chmod(path, 0o600)


def handle_qr(info: dict) -> None:
    url = info.get("url") or ""
    expire = info.get("expire_in")
    print("== Scan with the Feishu / Lark mobile app to create the app ==")
    print(f"QR URL: {url}")
    if expire:
        print(f"Expires in {expire} seconds. The scanning user becomes the app owner/admin.")
    try:
        import qrcode  # optional dependency (requirements: qrcode[pil])

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        try:
            qr.print_ascii(invert=True)
        except Exception:
            pass
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        saved = None
        try:
            img = qr.make_image(fill_color="black", back_color="white")
            img.save(QR_PNG)
            os.chmod(QR_PNG, 0o600)
            saved = QR_PNG
        except Exception:
            try:
                import qrcode.image.svg

                img = qr.make_image(image_factory=qrcode.image.svg.SvgImage)
                img.save(QR_SVG)
                os.chmod(QR_SVG, 0o600)
                saved = QR_SVG
            except Exception:
                saved = None
        if saved is not None:
            print(f"QR image saved at: {saved} (show this file to the app owner)")
        else:
            print("QR image rendering unavailable (needs Pillow); use the terminal QR above or the URL.")
    except ImportError:
        print("python 'qrcode' package not installed; open the QR URL above on the")
        print("owner's phone, or install deps with: pip install 'qrcode[pil]'")


def handle_status(info: dict) -> None:
    print(f"status: {info.get('status')}", info.get("interval", ""))


def run_register(args) -> dict:
    mock = os.environ.get("SCAN_CREATE_MOCK")
    if mock:
        print(f"WARNING: SCAN_CREATE_MOCK is set ({mock}); SDK flow skipped (test only).")
        return json.loads(Path(mock).read_text(encoding="utf-8"))
    try:
        import lark_oapi as lark
    except ImportError:
        print("lark_oapi is not installed. Install deps first: "
              "pip install -r requirements.txt  (needs lark-oapi >= 1.5.5)", file=sys.stderr)
        sys.exit(2)
    kwargs = {"on_qr_code": handle_qr, "on_status_change": handle_status,
              "addons": ADDONS, "source": "feishu-bridge"}
    preset = {}
    if args.name:
        preset["name"] = args.name
    if args.desc:
        preset["desc"] = args.desc
    if preset:
        kwargs["app_preset"] = preset
    if args.app_id:
        kwargs["app_id"] = args.app_id
    elif args.create_only:
        kwargs["create_only"] = True
    return lark.register_app(**kwargs)


def validate_token(api_host: str, app_id: str, app_secret: str) -> dict:
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(
        f"https://{api_host}/open-apis/auth/v3/tenant_access_token/internal",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    return {"code": data.get("code"), "msg": data.get("msg"), "expire": data.get("expire")}


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan-to-create a Feishu/Lark app for this bridge.")
    parser.add_argument("--name", default="", help="App name pre-filled on the confirm page (supports {user}).")
    parser.add_argument("--desc", default="", help="App description pre-filled on the confirm page.")
    parser.add_argument("--create-only", action="store_true",
                        help="Only allow creating a new app (hide the select-existing entry).")
    parser.add_argument("--app-id", default="",
                        help="Update an existing app (cli_...) instead of creating a new one.")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite an existing non-empty credentials.json.")
    args = parser.parse_args()

    if CRED_PATH.exists() and not args.force:
        try:
            existing = json.loads(CRED_PATH.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        if (existing.get("app_id") or "").strip():
            print("credentials.json already holds an app_id; refusing to overwrite "
                  "(pass --force only if the owner wants to replace this bot).", file=sys.stderr)
            return 3

    try:
        result = run_register(args)
    except Exception as exc:  # SDK raises AppAccessDeniedError / AppExpiredError / RegisterAppError
        print(f"scan-create failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4
    finally:
        for qr_file in (QR_PNG, QR_SVG):
            if qr_file.exists():
                try:
                    qr_file.unlink()
                except OSError:
                    pass

    app_id = (result.get("client_id") or "").strip()
    secret_val = str(result.get("client_secret") or "").strip()
    if not app_id or not secret_val:
        print("scan-create returned incomplete credentials; nothing written.", file=sys.stderr)
        return 5
    user_info = result.get("user_info") or {}
    open_id = (user_info.get("open_id") or "").strip()
    brand = (user_info.get("tenant_brand") or "feishu").strip().lower()
    is_lark = brand == "lark"
    api_host = "open.larksuite.com" if is_lark else "open.feishu.cn"

    write_json_0600(CRED_PATH, {"app_id": app_id, "app_secret": secret_val})

    config = {}
    if CONFIG_PATH.exists():
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            config = {}
    config["app_id"] = app_id
    config["base"] = "lark" if is_lark else "feishu"
    config["api_host"] = api_host
    if open_id:
        allowed = list(config.get("allowed_open_ids") or [])
        if open_id not in allowed:
            allowed.append(open_id)
        config["allowed_open_ids"] = allowed
    write_json_0600(CONFIG_PATH, config)

    summary = {
        "app_id": app_id,
        "tenant_brand": brand,
        "api_host": api_host,
        "credentials_file": str(CRED_PATH),
        "credentials_mode": "local_file (0600)",
        "owner_open_id_whitelisted": bool(open_id),
    }
    try:
        summary["token_check"] = validate_token(api_host, app_id, secret_val)
    except Exception as exc:
        summary["token_check"] = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("Next: follow README.md (the deployment guide) from the startup smoke test onward. If the tenant")
    print("requires admin approval or a version publish, finish that in the developer")
    print("console; if messages do not arrive, verify the im.message.receive_v1 event")
    print("is subscribed over the long-connection (WebSocket) mode.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
