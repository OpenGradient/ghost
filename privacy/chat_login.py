#!/usr/bin/env python3
"""ghost-login -- connect ghost to your OpenGradient Chat account.

Usual workflow (browser):
    1. Spin up a one-shot loopback listener on 127.0.0.1.
    2. Open the website's /cli-auth page, pointed back at that listener.
    3. You log in on the website (or you're already signed in) and click
       "Authorize this device".
    4. The page POSTs your Supabase session + public config to the listener,
       which stores it at ~/.ghost/privacy/chat_auth.json.

The hosted-model path (scrubbing_proxy.py) then uses that token as the Supabase
bearer for chat-api's OHTTP endpoint, refreshing it automatically as it ages.

Fallback (no browser / headless): `ghost-login --paste` reads the bundle JSON
(copied from the website's "Copy" button) from stdin.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from chat_auth import AUTH_FILE, load_auth, save_auth

# The public website that serves /cli-auth. Override for staging/self-hosted.
DEFAULT_CHAT_APP_URL = "https://chat.opengradient.ai"


def _website_url() -> str:
    return os.environ.get("GHOST_CHAT_APP_URL", DEFAULT_CHAT_APP_URL).rstrip("/")


def _valid_bundle(data: object) -> bool:
    return (
        isinstance(data, dict)
        and data.get("type") == "opengradient-cli-auth"
        and isinstance(data.get("access_token"), str)
        and isinstance(data.get("refresh_token"), str)
        and isinstance(data.get("config"), dict)
    )


def _summary(bundle: dict) -> str:
    user = bundle.get("user") or {}
    who = user.get("email") or ("guest" if user.get("is_anonymous") else user.get("id", "?"))
    cfg = bundle.get("config") or {}
    return f"signed in as {who} · chat-api {cfg.get('chat_api_base_url', '?')}"


def _run_browser_flow() -> int:
    received: dict = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # silence default logging
            pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(n) if n else b""
            ok = False
            try:
                data = json.loads(raw)
                if _valid_bundle(data):
                    save_auth(data)
                    received.update(data)
                    ok = True
            except Exception:
                ok = False
            payload = b'{"ok":true}' if ok else b'{"ok":false}'
            self.send_response(200 if ok else 400)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            if ok:
                done.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    redirect_uri = f"http://127.0.0.1:{port}/callback"
    url = f"{_website_url()}/cli-auth?redirect_uri={redirect_uri}"
    print("👻 ghost-login")
    print(f"   Opening {url}")
    print("   Log in (if needed) and click “Authorize this device”.")
    print("   Waiting for the browser to hand back your session… (Ctrl-C to cancel)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    print(f"\n   If your browser didn't open, visit:\n   {url}\n")

    try:
        if not done.wait(timeout=300):
            print("   Timed out waiting for authorization. Try again, or use `ghost-login --paste`.", file=sys.stderr)
            return 1
    except KeyboardInterrupt:
        print("\n   Cancelled.", file=sys.stderr)
        return 1
    finally:
        server.shutdown()

    print(f"\n   ✅ Connected — {_summary(received)}")
    print(f"   Token stored at {AUTH_FILE}")
    return 0


def _run_paste_flow() -> int:
    print("Paste the cli-auth token JSON (from the website's Copy button), then Ctrl-D:", file=sys.stderr)
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("   ✗ That wasn't valid JSON.", file=sys.stderr)
        return 1
    if not _valid_bundle(data):
        print("   ✗ That doesn't look like a cli-auth token bundle.", file=sys.stderr)
        return 1
    save_auth(data)
    print(f"   ✅ Connected — {_summary(data)}")
    return 0


def main(argv: list[str]) -> int:
    if "--status" in argv:
        bundle = load_auth()
        if bundle and bundle.get("access_token"):
            print(f"Logged in — {_summary(bundle)}")
            return 0
        print("Not logged in. Run `ghost-login`.")
        return 1
    if "--logout" in argv:
        try:
            os.remove(AUTH_FILE)
            print("Logged out.")
        except FileNotFoundError:
            print("Already logged out.")
        return 0
    if "--paste" in argv:
        return _run_paste_flow()
    return _run_browser_flow()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
