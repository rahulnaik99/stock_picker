"""Generate a fresh FYERS v3 access token (via the official fyers-apiv3 SDK) and write it into .env.

FYERS access tokens expire DAILY, so run this each day before using live market data:

    python -m scripts.fyers_login

One-time setup in .env (from https://myapi.fyers.in/dashboard/):
    FYERS_APP_ID        App ID / client id, e.g. XC4XXXXM-100
    FYERS_SECRET_ID     the app's Secret ID
    FYERS_REDIRECT_URI  the redirect URI registered on the app (must match EXACTLY)

The script prints a login URL; log in and approve, then paste the redirected URL (it contains ?auth_code=...).
The token is exchanged and written to FYERS_ACCESS_TOKEN in .env. FYERS is the only market-data source (no fallback),
so a valid token here is required for candles/option-chain; without it those agents report unavailable.
"""
from __future__ import annotations

import re
import sys
from urllib.parse import parse_qs, urlparse

from app.service.settings import ROOT, get_settings

ENV_PATH = ROOT / ".env"


def _extract_auth_code(pasted: str) -> str:
    pasted = pasted.strip()
    if "auth_code=" in pasted:
        qs = parse_qs(urlparse(pasted).query)
        if qs.get("auth_code"):
            return qs["auth_code"][0]
    return pasted


def _write_env_token(token: str) -> None:
    line = f"FYERS_ACCESS_TOKEN={token}"
    if ENV_PATH.exists():
        text = ENV_PATH.read_text(encoding="utf-8")
        text = re.sub(r"(?m)^FYERS_ACCESS_TOKEN=.*$", line, text) if re.search(r"(?m)^FYERS_ACCESS_TOKEN=.*$", text) \
            else text.rstrip("\n") + "\n" + line + "\n"
    else:
        text = line + "\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def main() -> int:
    s = get_settings()
    missing = [k for k, v in (("FYERS_APP_ID", s.fyers_app_id), ("FYERS_SECRET_ID", s.fyers_secret_id),
                              ("FYERS_REDIRECT_URI", s.fyers_redirect_uri)) if not v]
    if missing:
        print("Missing in .env: " + ", ".join(missing))
        print("Add them from https://myapi.fyers.in/dashboard/ (redirect URI must match the app exactly), then re-run.")
        return 1
    try:
        from fyers_apiv3 import fyersModel
    except ImportError:
        print("fyers-apiv3 is not installed. Run:  pip install fyers-apiv3")
        return 1

    session = fyersModel.SessionModel(
        client_id=s.fyers_app_id, secret_key=s.fyers_secret_id, redirect_uri=s.fyers_redirect_uri,
        response_type="code", grant_type="authorization_code")
    print("\n1) Open this URL, log in and approve:\n\n   " + session.generate_authcode())
    print("\n2) Your browser is redirected to your redirect URI with ?auth_code=... in the address bar.")
    pasted = input("\n3) Paste the full redirected URL (or just the auth_code) here:\n> ")
    auth_code = _extract_auth_code(pasted)
    if not auth_code:
        print("No auth_code found in what you pasted.")
        return 1

    session.set_token(auth_code)
    resp = session.generate_token() or {}
    token = resp.get("access_token")
    if not token:
        print(f"FYERS did not return an access token: {resp}")
        return 1

    _write_env_token(token)
    print(f"\nFYERS_ACCESS_TOKEN updated in {ENV_PATH}. Valid for today only — re-run tomorrow. Restart the app to pick it up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
