#!/usr/bin/env python3
"""ghost-login: connect ghost to DreamFort, with your account or an API key.

    ghost-login                  choose how to log in
    ghost-login --account        your DreamFort account (your plan and credits)
    ghost-login --api-key        a DreamFort API key (billed by usage)
    ghost-login --with-api-key   the same, reading the key from stdin
    ghost-login --device         account login by typing a short code on the website
    ghost-login --browser        account login through this machine's browser
    ghost-login --paste          account login by pasting the token the website shows
    ghost-login --status         who you're logged in as
    ghost-login --logout         log out
"""
import argparse
import base64
import json
import os
import select
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlencode, urlsplit

import httpx

import ghost_auth
from ghost_auth import AuthError
from localhttp import LocalServer

APP_URL = os.environ.get("GHOST_CHAT_APP_URL", "https://www.dreamfort.ai").rstrip("/")
KEYS_URL = f"{ghost_auth.CONSOLE_URL}/api-keys"
TIMEOUT = httpx.Timeout(15.0)
LOGIN_WINDOW = 600.0
MIN_POLL = 1.0
LOCAL = 3   # exit code: they chose the offline model instead of logging in

TTY = sys.stdin.isatty() and sys.stdout.isatty()
COLOR = TTY and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"

ART = """\
   ▄████████████▄
   ██████████████
   ███  ████  ███
   ███  ████  ███
   ██████████████
   ██████████████
   ██▀██▀██▀██▀██"""


class Cancelled(Exception):
    pass


# ── terminal ──────────────────────────────────────────────────────────────────
def style(text, code):
    return f"\x1b[{code}m{text}\x1b[0m" if COLOR else text


def gold(text):
    return style(text, "38;5;220")


def bold(text):
    return style(text, "1")


def dim(text):
    return style(text, "2")


def green(text):
    return style(text, "32")


def red(text):
    return style(text, "31")


def say(text=""):
    print(f" {text}" if text else "", flush=True)


class Keys:
    """Keypresses without waiting for Enter (so a long paste isn't cut at the terminal's line limit). Nests:
    a login is one of these from start to end, so nothing typed between two prompts is echoed or lost."""
    _depth, _fd, _saved = 0, None, None

    def __enter__(self):
        if Keys._depth == 0 and TTY and os.name != "nt":
            import termios
            import tty
            Keys._fd = sys.stdin.fileno()
            Keys._saved = termios.tcgetattr(Keys._fd)
            tty.setcbreak(Keys._fd, termios.TCSANOW)
        Keys._depth += 1
        return self

    def __exit__(self, *exc):
        Keys._depth -= 1
        if Keys._depth == 0 and Keys._saved is not None:
            import termios
            termios.tcsetattr(Keys._fd, termios.TCSANOW, Keys._saved)
            Keys._saved = None

    def read(self, timeout):
        """What was typed or pasted within `timeout` seconds ('' if nothing)."""
        if not TTY:
            time.sleep(timeout)
            return ""
        if os.name == "nt":
            return self._read_windows(timeout)
        if not select.select([self._fd], [], [], timeout)[0]:
            return ""
        data = os.read(self._fd, 65536)
        while select.select([self._fd], [], [], 0.02)[0]:
            more = os.read(self._fd, 65536)
            if not more:
                break
            data += more
        return data.decode("utf-8", "replace")

    @staticmethod
    def _read_windows(timeout):
        import msvcrt
        deadline, out = time.monotonic() + timeout, ""
        while time.monotonic() < deadline or (out and msvcrt.kbhit()):
            if not msvcrt.kbhit():
                time.sleep(0.02)
                continue
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):
                ch = {"H": "\x1b[A", "P": "\x1b[B"}.get(msvcrt.getwch(), "")
            if ch == "\x03":
                raise KeyboardInterrupt
            out += ch
            if not msvcrt.kbhit():
                break
        return out


def choose(options):
    """A menu of (title, note). Returns the index. Arrow keys or a number, Enter to confirm, Esc to cancel."""
    at, drawn = 0, False
    with Keys() as keys:
        while True:
            if drawn and COLOR:
                sys.stdout.write(f"\x1b[{len(options)}A")
            for i, (title, note) in enumerate(options):
                mark = gold("❯") if i == at else " "
                name = f"{i + 1}. {title}"
                line = f" {mark} {gold(name) if i == at else name} {dim('· ' + note)}"
                sys.stdout.write(("\x1b[2K" if COLOR else "") + line + "\n")
            sys.stdout.flush()
            drawn = True
            while not (key := keys.read(0.5)):
                pass
            if key in ("\x1b[A", "\x1bOA", "k"):
                at = (at - 1) % len(options)
            elif key in ("\x1b[B", "\x1bOB", "j", "\t"):
                at = (at + 1) % len(options)
            elif key.isdigit() and 1 <= int(key) <= len(options):
                return int(key) - 1
            elif key in ("\r", "\n"):
                return at
            elif key in ("\x1b", "q"):
                raise Cancelled


def ask_secret(prompt):
    """A line typed or pasted without showing it."""
    if not TTY:
        return sys.stdin.readline().strip()
    value = ""
    with Keys() as keys:
        while True:
            shown = "•" * min(len(value), 40)
            sys.stdout.write(f"\r{chr(27) + '[2K' if COLOR else ''} {prompt}{shown}")
            sys.stdout.flush()
            text = keys.read(0.5)
            if text in ("\x1b",):
                print()
                raise Cancelled
            text = text.replace("\x1b[200~", "").replace("\x1b[201~", "")
            for ch in text:
                if ch in ("\r", "\n"):
                    if value.strip():
                        print(flush=True)
                        return value.strip()
                elif ch in ("\x7f", "\b"):
                    value = value[:-1]
                elif ch == "\x15":
                    value = ""
                elif ch.isprintable():
                    value += ch


class Spinner:
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, text):
        self.text, self.n, self.next = text, 0, 0.0

    def tick(self):
        if COLOR and time.monotonic() >= self.next:
            sys.stdout.write(f"\r\x1b[2K {gold(self.FRAMES[self.n % len(self.FRAMES)])} {self.text}")
            sys.stdout.flush()
            self.n, self.next = self.n + 1, time.monotonic() + 0.1

    def clear(self):
        if COLOR:
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()


def open_browser(url):
    """Best effort, and only where a window can open in front of you (not over SSH, nor in a text browser)."""
    headless = sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if headless or os.environ.get("SSH_CONNECTION") or os.environ.get("GHOST_NO_OPEN"):
        return
    quiet = subprocess.DEVNULL   # browsers like to talk
    try:
        subprocess.Popen([sys.executable, "-Es", "-m", "webbrowser", url], stdin=quiet, stdout=quiet, stderr=quiet)
    except OSError:
        pass


# ── account ───────────────────────────────────────────────────────────────────
def parse_pasted(text):
    """The login bundle in pasted text: the JSON the website shows, or that JSON in base64. None if it isn't (yet)."""
    text = text.replace("\x1b[200~", "").replace("\x1b[201~", "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return None
    if len(text) > 100:
        try:
            return json.loads(base64.b64decode("".join(text.split()), validate=True))
        except ValueError:
            return None
    return None


def device_code():
    """Ask DreamFort for a login code. None when this server doesn't do code logins."""
    resp = httpx.post(f"{ghost_auth.CHAT_API_URL}/api/v1/cli-auth/device/code", timeout=TIMEOUT)
    if resp.status_code in (404, 405, 501):
        return None
    resp.raise_for_status()
    return resp.json()


def device_login(handshake):
    """Wait for the code to be approved on the website; returns the login bundle."""
    url = handshake["verificationUri"]
    say("Enter this code on the website to sign in:")
    say()
    say(f"   {bold(gold(handshake['userCode']))}")
    say()
    say(f"   {url}")
    say()
    say(dim("Only enter a code you just saw in your own terminal."))
    say()
    open_browser(url)
    spin = Spinner(f"Waiting for you to approve it… {dim('(Esc to cancel)')}")
    if not COLOR:
        say("Waiting for you to approve it…")
    deadline = time.monotonic() + float(handshake["expiresIn"])
    interval = max(MIN_POLL, float(handshake["interval"]))
    next_poll = time.monotonic() + interval
    with Keys() as keys:
        while time.monotonic() < deadline:
            spin.tick()
            if keys.read(0.1) == "\x1b":
                spin.clear()
                raise Cancelled
            if time.monotonic() < next_poll:
                continue
            next_poll = time.monotonic() + interval
            try:
                poll = httpx.post(f"{ghost_auth.CHAT_API_URL}/api/v1/cli-auth/device/token",
                                  json={"deviceCode": handshake["deviceCode"]}, timeout=TIMEOUT)
            except httpx.HTTPError:
                continue   # a network blip; keep waiting
            if poll.status_code == 200:
                spin.clear()
                return poll.json()
            try:
                error = poll.json().get("error")
            except ValueError:
                error = None
            if error in ("expired_token", "invalid_grant"):
                break
            if error == "slow_down" or poll.status_code == 429:
                interval += 5
            elif error != "authorization_pending" and poll.status_code < 500:
                spin.clear()
                raise AuthError(f"the website answered unexpectedly ({poll.status_code})")
    spin.clear()
    raise AuthError("that code expired before it was approved; run `ghost-login` to get a new one")


def _loopback(received):
    """A one-shot server on this machine for the website to hand the login to."""
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(APP_URL))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, status, body=b"{}"):
            self.send_response(status)
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")   # Chrome asks before a site may reach this machine
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self._reply(204, b"")

        def do_POST(self):
            sender = self.headers.get("Origin")
            raw = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 1 << 20))
            if sender not in (None, origin):   # another page on this machine must not log you in as someone else
                return self._reply(403)
            try:
                received.append(json.loads(raw))
            except ValueError:
                return self._reply(400, b'{"ok": false}')
            self._reply(200, b'{"ok": true}')

    return LocalServer(("127.0.0.1", 0), Handler)


def browser_login(*, paste_only=False):
    """Sign in on the website, which hands the login back to this machine, or shows it to paste."""
    received = []
    server = None
    query = {"app_name": "ghost"}
    if not paste_only:
        server = _loopback(received)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        query["redirect_uri"] = f"http://127.0.0.1:{server.server_port}"
    url = f"{APP_URL}/cli-auth?{urlencode(query)}"
    if paste_only:
        say("Sign in here, then paste the token it shows you:")
    else:
        say("Opening your browser to sign in…")
        say()
        say("Browser didn't open? Use this address:")
    say()
    say(f"   {url}")
    say()
    open_browser(url)
    prompt = "Paste the token here: " if paste_only else "Waiting… (or paste the token the page shows; Esc to cancel)"
    say(prompt) if not COLOR else None
    spin = Spinner(prompt)
    pasted, deadline = "", time.monotonic() + LOGIN_WINDOW
    try:
        with Keys() as keys:
            while time.monotonic() < deadline:
                if not pasted:
                    spin.tick()
                text = keys.read(0.1)
                if received:
                    spin.clear()
                    return received[0]
                if text == "\x1b":
                    spin.clear()
                    raise Cancelled
                if text:
                    pasted += text
                    bundle = parse_pasted(pasted)
                    if bundle is not None:
                        spin.clear()
                        return bundle
                    if len(pasted) > 1 << 18 or (text in ("\r", "\n") and len(pasted) < 100):
                        pasted = ""
    finally:
        if server:
            server.shutdown()
            server.server_close()
    spin.clear()
    raise AuthError("nobody signed in within 10 minutes; run `ghost-login` to try again")


def account_login(how=None):
    if how in (None, "device"):
        try:
            handshake = device_code()
        except httpx.HTTPError as e:
            if how == "device":
                raise AuthError(f"couldn't reach DreamFort ({type(e).__name__}); check your connection and try again") from e
            handshake = None
        if handshake:
            bundle = device_login(handshake)
        elif how == "device":
            raise AuthError("DreamFort doesn't offer code logins yet; run `ghost-login` to use your browser")
        else:
            bundle = browser_login()
    else:
        bundle = browser_login(paste_only=how == "paste")
    ghost_auth.save_account(bundle)
    ghost_auth.forget_api_key()


# ── API key ───────────────────────────────────────────────────────────────────
def check_key(key):
    """Raise AuthError unless DreamFort accepts this key. Returns a note about its credits, or None."""
    if not key.startswith(ghost_auth.API_KEY_PREFIX):
        raise AuthError(f"DreamFort API keys start with {ghost_auth.API_KEY_PREFIX}")
    headers = {"Authorization": f"Bearer {key}"}
    relay = ghost_auth.CHAT_API_URL + ghost_auth.API_KEY_RELAY
    try:
        resp = httpx.get(f"{relay}/config", headers=headers, timeout=TIMEOUT)
        if resp.status_code in (401, 403):
            raise AuthError("DreamFort doesn't accept that key; check it was copied whole and hasn't been revoked")
        if resp.status_code >= 400:
            raise AuthError(f"DreamFort couldn't check that key right now ({resp.status_code}); try again in a moment")
        # An empty request is refused before anything runs, but only after the balance is checked.
        probe = httpx.post(relay, headers={**headers, "Content-Type": "message/ohttp-req", "X-TEE-ID": "0x0"}, timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise AuthError(f"couldn't reach DreamFort ({type(e).__name__}); check your connection and try again") from e
    return "This key works, but its account has no credits yet." if probe.status_code == 402 else None


def api_key_login(key=None):
    interactive = key is None
    if interactive:
        say(f"Create a key at {KEYS_URL} and paste it here.")
        say()
    while True:
        value = key if key is not None else ask_secret("DreamFort API key: ")
        try:
            note = check_key(value)
            break
        except AuthError as e:
            if not interactive or not TTY:
                raise
            say(red(f"✗ {e}"))
            say()
    ghost_auth.save_api_key(value)
    if note:
        say(f"! {note} Add some at {ghost_auth.CONSOLE_URL}")


# ── commands ──────────────────────────────────────────────────────────────────
def welcome():
    print()
    print(gold(ART))
    print()
    say(bold("Welcome to ghost"))
    print()


def interactive(local_model):
    say("ghost can be used with your DreamFort account or billed by usage with a DreamFort API key.")
    say()
    say(bold("Select login method:"))
    say()
    options = [("DreamFort account", "your plan and credits"), ("DreamFort API key", "billed by usage")]
    if local_model:
        options.append(("Local model", "fully offline, nothing leaves this machine"))
    picked = choose(options)
    say()
    if picked == 2:
        return "local"
    account_login() if picked == 0 else api_key_login()
    return "hosted"


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):   # a Windows pipe or an ASCII locale can't take a ✓ otherwise
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="ghost-login", description="Connect ghost to DreamFort.")
    how = parser.add_mutually_exclusive_group()
    for flag, text in (("account", "log in with your DreamFort account"), ("api-key", "log in with a DreamFort API key"),
                       ("with-api-key", "log in with an API key read from stdin"), ("device", "account login with a code typed on the website"),
                       ("browser", "account login through this machine's browser"), ("paste", "account login by pasting the website's token"),
                       ("status", "show who you're logged in as"), ("logout", "log out")):
        how.add_argument(f"--{flag}", action="store_true", help=text)
    parser.add_argument("--first-run", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--local-model", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.status:
        who = ghost_auth.label()
        print(f"Logged in with your {who}" if who else "Not logged in. Run: ghost-login")
        return 0 if who else 1
    if args.logout:
        who = ghost_auth.label()
        ghost_auth.log_out()
        print(f"Logged out of your {who}" if who else "You weren't logged in.")
        return 0

    if not TTY and not (args.with_api_key or args.account or args.api_key or args.device or args.browser or args.paste):
        print("ghost-login needs a terminal. From a script, use: ghost-login --with-api-key < keyfile", file=sys.stderr)
        return 1
    try:
        with Keys() as keys:
            if args.with_api_key:
                api_key_login(sys.stdin.readline().strip())
            elif args.api_key:
                api_key_login()
            elif args.account or args.device or args.browser or args.paste:
                account_login("device" if args.device else "browser" if args.browser else "paste" if args.paste else None)
            else:
                welcome() if args.first_run else print()
                if interactive(args.local_model) == "local":
                    return LOCAL
            say(green(f"✓ Logged in with your {ghost_auth.label()}"))
            if args.first_run and TTY:
                say(dim("Press Enter to continue…"))
                while not set(keys.read(0.5)) & {"\r", "\n"}:
                    pass
                sys.stdout.write("\x1b[2J\x1b[3J\x1b[H" if COLOR else "\n")   # the chat starts on a clean screen
                sys.stdout.flush()
        return 0
    except AuthError as e:
        say(red(f"✗ {e}"))
        return 1
    except (Cancelled, KeyboardInterrupt):
        print()
        say("Login cancelled. Run `ghost-login` when you're ready.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
