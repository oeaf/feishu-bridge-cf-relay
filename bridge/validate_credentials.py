"""Validate a credentials file by exchanging a tenant access token.

Prints only code / msg / expire. Never prints the secret or the token.
Usage: validate_credentials.py [--credentials-file <path>]
Default credentials path: $FEISHU_BRIDGE_HOME/credentials.json, else
<repo root>/credentials.json.
"""
import argparse
import json
import os
import urllib.request
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = Path(os.environ.get("FEISHU_BRIDGE_HOME") or CODE_DIR).resolve()
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credentials-file", default=str(BASE_DIR / "credentials.json"))
    args = parser.parse_args()

    creds = json.loads(Path(args.credentials_file).read_text(encoding="utf-8"))
    body = json.dumps(
        {"app_id": creds["app_id"], "app_secret": creds["app_secret"]}
    ).encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": BROWSER_UA},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    print(json.dumps({"code": data.get("code"), "msg": data.get("msg"),
                      "expire": data.get("expire")}))
    return 0 if data.get("code") == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
