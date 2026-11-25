"""ghost-login --device: log in by typing a short code into the website.

For machines the browser can't call back to (SSH, containers, a firewalled
loopback): no token JSON, no pasting. We ask chat-api for a device-login
handshake, print an 8-char code, and poll until the user approves it on the
site; the approved CLI-auth bundle is then handed to og-veil's own manual
login, exactly like `ghost-login --paste`.

Run by ghost-login with the og-veil venv's python (needs requests + veil).
"""

import os
import sys
import time
import webbrowser

import requests
from veil.session import AuthError, login_manual

TIMEOUT = 15


def main() -> int:
    api = os.environ.get("GHOST_CHAT_API_URL", "https://chat-api.memchat.io").rstrip("/")
    try:
        opened = requests.post(f"{api}/api/v1/cli-auth/device/code", timeout=TIMEOUT)
        opened.raise_for_status()
        handshake = opened.json()
    except requests.RequestException as exc:
        print(f"couldn't reach {api} to start device login: {exc}", file=sys.stderr)
        return 1

    device_code = handshake["deviceCode"]
    print(f"\n  1. Open  {handshake['verificationUri']}")
    print(f"  2. Enter code:  {handshake['userCode']}\n")
    try:
        webbrowser.open(handshake["verificationUri"])
    except Exception:
        pass  # headless: the URL above is enough

    deadline = time.monotonic() + float(handshake["expiresIn"])
    interval = float(handshake["interval"])
    while time.monotonic() < deadline:
        time.sleep(interval)
        print("  waiting for approval...", end="\r")
        try:
            poll = requests.post(
                f"{api}/api/v1/cli-auth/device/token",
                json={"deviceCode": device_code},
                timeout=TIMEOUT,
            )
        except requests.RequestException:
            continue  # transient network blip; keep waiting until the deadline
        if poll.status_code == 200:
            try:
                session = login_manual(poll.text)
            except AuthError as exc:
                print(f"\nlogin failed: {exc}", file=sys.stderr)
                return 1
            print(f"\n✓ Logged in as {session.user_email}")
            return 0
        error = (poll.json() if poll.content else {}).get("error", "")
        if error == "authorization_pending":
            continue
        if error == "expired_token":
            print("\nthe code expired — run ghost-login --device again", file=sys.stderr)
            return 1
        print(f"\nunexpected answer from {api}: {poll.status_code} {poll.text}", file=sys.stderr)
        return 1

    print("\ntimed out waiting for approval — run ghost-login --device again", file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ncancelled")
        sys.exit(130)
