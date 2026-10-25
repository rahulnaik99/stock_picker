"""Generate a fresh FYERS v3 access token and write it into .env.

WHY THIS EXISTS
FYERS access tokens expire DAILY. You cannot just paste a token into .env and expect it to keep working —
each day you must complete the login flow to mint a new one. This script does that:

    python -m scripts.fyers_login

What you need first (one-time), from https://myapi.fyers.in/dashboard/ :
    FYERS_APP_ID        your App ID / client id, e.g. ABCD1234-100
    FYERS_SECRET_ID     the app's Secret ID
    FYERS_REDIRECT_URI  the redirect URI registered on the app (must match EXACTLY)

Put those three in .env, then run this script. It prints a login URL; log in, approve, and your browser is
redirected to your redirect URI with `?auth_code=...` in the address bar. Paste that full redirected URL (or just
the auth_code) back here. The script exchanges it for an access token and updates FYERS_ACCESS_TOKEN in .env.

Endpoints follow the FYERS API v3 docs; if FYERS changes them, update the two URLs below.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from app.service.settings import ROOT, get_settings

AUTHCODE_URL = "https://api-t1.fyers.in/api/v3/generate-authcode"
VALIDATE_URL = "https://api-t1.fyers.in/api/v3/validate-authcode"
ENV_PATH = ROOT / ".env"


def _extract_auth_code(pasted: str) -> str:
    pasted = pasted.strip()
    if "auth_code=" in pasted:
        qs = parse_qs(urlparse(pasted).query)
        if qs.get("auth_code"):
            return qs["auth_code"][0]
    return pasted  # assume the user pasted the bare auth_code


def _write_env_token(token: str) -> None:
    """Update (or append) FYERS_ACCESS_TOKEN in .env without touching other lines."""
    line = f"FYERS_ACCESS_TOKEN={token}"
    if ENV_PATH.exists():
        text = ENV_PATH.read_text(encoding="utf-8")
        if re.search(r"(?m)^FYERS_ACCESS_TOKEN=.*$", text):
            text = re.sub(r"(?m)^FYERS_ACCESS_TOKEN=.*$", line, text)
        else:
            text = text.rstrip("\n") + "\n" + line + "\n"
    else:
        text = line + "\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def main() -> int:
    s = get_settings()
    missing = [k for k, v in (("FYERS_APP_ID", s.fyers_app_id), ("FYERS_SECRET_ID", s.fyers_secret_id),
                              ("FYERS_REDIRECT_URI", s.fyers_redirect_uri)) if not v]
    if missing:
        print("Missing in .env: " + ", ".join(missing))
        print("Add them from https://myapi.fyers.in/dashboard/ (the redirect URI must match the app exactly), then re-run.")
        return 1

    login_url = (f"{AUTHCODE_URL}?client_id={s.fyers_app_id}&redirect_uri={s.fyers_redirect_uri}"
                 "&response_type=code&state=ekx")
    print("\n1) Open this URL, log in and approve:\n")
    print("   " + login_url)
    print("\n2) Your browser will be redirected to your redirect URI with ?auth_code=... in the address bar.")
    pasted = input("\n3) Paste the full redirected URL (or just the auth_code) here:\n> ")
    auth_code = _extract_auth_code(pasted)
    if not auth_code:
        print("No auth_code found in what you pasted.")
        return 1

    app_id_hash = hashlib.sha256(f"{s.fyers_app_id}:{s.fyers_secret_id}".encode()).hexdigest()
    try:
        resp = httpx.post(VALIDATE_URL, json={"grant_type": "authorization_code", "appIdHash": app_id_hash, "code": auth_code}, timeout=30)
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        print(f"Token exchange failed: {exc}")
        return 1

    token = body.get("access_token")
    if not token:
        print(f"FYERS did not return an access token: {body}")
        return 1

    _write_env_token(token)
    print(f"\nFYERS_ACCESS_TOKEN updated in {ENV_PATH}. It is valid for today only — re-run this script tomorrow.")
    print("Restart the API / rerun so the new token is picked up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
