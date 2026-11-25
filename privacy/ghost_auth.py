"""How this machine is logged in to DreamFort. Shared by the bridge and ghost-login.

  account   your DreamFort plan and credits. The website hands over a session, which is kept in the file
            og-veil uses (~/.opengradient/veil/session.json), so the two share one login.
  api key   a `df_sk_` developer key in ~/.ghost/privacy/dreamfort_api_key, billed by usage.

Both files are 0600. An API key wins while both are present; logging in with an account removes it.

A login says who you are, never where your requests go or which enclaves to trust: those are fixed here and
in opengradient-ohttp, so a login you were tricked into accepting can't point ghost at an impostor.
"""
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

API_KEY_PREFIX = "df_sk_"
CHAT_API_URL = os.environ.get("GHOST_CHAT_API_URL", "https://chat-api.memchat.io").rstrip("/")
ACCOUNT_RELAY, API_KEY_RELAY = "/api/v1/chat/ohttp", "/api/v1/developer/ohttp"
BUNDLE_TYPE = "opengradient-cli-auth"   # what the website's /cli-auth page hands to a CLI

KEY_FILE = Path(os.environ.get("GHOST_HOME") or Path.home() / ".ghost") / "privacy" / "dreamfort_api_key"
BRIDGE_KEY_FILE = KEY_FILE.with_name("bridge.key")
SESSION_FILE = Path(os.environ.get("OG_VEIL_HOME") or Path.home() / ".opengradient" / "veil") / "session.json"

CREDITS_URL = "https://www.dreamfort.ai/dashboard/credits"
CONSOLE_URL = "https://api.dreamfort.ai"
NOT_LOGGED_IN = "not logged in to DreamFort; run `ghost-login`"
EXPIRED = "your DreamFort login has expired; run `ghost-login` to connect again"


class AuthError(Exception):
    """Nobody is logged in, or the login no longer works."""


_lock = threading.RLock()
_cached = {}  # path -> (mtime_ns, value)


def _read(path, parse):
    """`parse(text)` of a small file, re-read only when it changes. None when missing or unreadable."""
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        return None
    with _lock:
        hit = _cached.get(path)
        if not hit or hit[0] != stamp:
            try:
                value = parse(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                value = None
            hit = _cached[path] = (stamp, value)
        return hit[1]


def _write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def bridge_proof(nonce):
    """What only your own bridge can answer. Its port may be held by another program, or by the ghost of someone
    else on this machine, and neither may have your prompts."""
    try:
        key = BRIDGE_KEY_FILE.read_text(encoding="utf-8")
    except OSError:
        key = secrets.token_hex(32)
        _write_private(BRIDGE_KEY_FILE, key)
    return hmac.new(key.encode(), nonce.encode(), "sha256").hexdigest()


# ── API key ───────────────────────────────────────────────────────────────────
def api_key():
    key = _read(KEY_FILE, str.strip)
    return key if key and key.startswith(API_KEY_PREFIX) else None


def save_api_key(key):
    _write_private(KEY_FILE, key.strip() + "\n")


def redacted(key):
    return f"{API_KEY_PREFIX}…{key[-4:]}"


# ── account ───────────────────────────────────────────────────────────────────
def account():
    """The account session as stored (a dict), or None."""
    session = _read(SESSION_FILE, json.loads)
    return session if isinstance(session, dict) and session.get("access_token") else None


def save_account(bundle):
    """Store the session the website handed over. Raises AuthError if it isn't one."""
    if not isinstance(bundle, dict) or bundle.get("type") != BUNDLE_TYPE:
        raise AuthError("that isn't a DreamFort login")
    config = bundle.get("config")
    if not bundle.get("access_token") or not isinstance(config, dict) or not config.get("supabase_url"):
        raise AuthError("that login is incomplete")
    _write_private(SESSION_FILE, json.dumps(bundle, indent=2))


def _expired(session):
    at = session.get("expires_at")
    return isinstance(at, (int, float)) and time.time() >= at - 60


def _refresh(session):
    config = session.get("config") or {}
    if not session.get("refresh_token") or not config.get("supabase_url"):
        raise AuthError(EXPIRED)
    import httpx   # only here: the launcher reads this module on every start
    import outbound
    try:
        resp = httpx.post(f"{config['supabase_url']}/auth/v1/token", params={"grant_type": "refresh_token"},
                          json={"refresh_token": session["refresh_token"]}, verify=outbound.trust(),
                          headers={"apikey": config.get("supabase_anon_key") or ""}, timeout=15)
    except httpx.HTTPError as e:
        raise AuthError(f"couldn't reach DreamFort to renew your login ({type(e).__name__}); check your connection") from e
    if resp.status_code in (400, 401, 403):
        # Another process sharing this file may have renewed it first, which retires our copy.
        with _lock:
            _cached.pop(SESSION_FILE, None)
        newer = account()
        if newer and newer["access_token"] != session["access_token"] and not _expired(newer):
            return newer
        raise AuthError(EXPIRED)
    try:
        resp.raise_for_status()
        body = resp.json()
        fresh = {**session, "access_token": body["access_token"], "refresh_token": body.get("refresh_token") or session["refresh_token"]}
    except (httpx.HTTPError, ValueError, KeyError) as e:
        raise AuthError("DreamFort couldn't renew your login; try again in a moment") from e
    fresh["expires_at"] = body.get("expires_at") or int(time.time()) + int(body.get("expires_in") or 3600)
    _write_private(SESSION_FILE, json.dumps(fresh, indent=2))
    return fresh


# ── both ──────────────────────────────────────────────────────────────────────
def mode():
    """'api_key', 'account' or None (not logged in). Reads local files only."""
    if api_key():
        return "api_key"
    return "account" if account() else None


def label():
    """Who we are logged in as, for people. None when nobody is."""
    key = api_key()
    if key:
        return f"DreamFort API key ({redacted(key)})"
    session = account()
    if not session:
        return None
    email = (session.get("user") or {}).get("email")
    return f"DreamFort account ({email})" if email else "DreamFort account"


def credentials():
    """(relay url, bearer token) for the login in use. Renews an account's token when it is due. Raises
    AuthError when there is nothing to use."""
    key = api_key()
    if key:
        return CHAT_API_URL + API_KEY_RELAY, key
    with _lock:
        session = account()
        if session is None:
            raise AuthError(NOT_LOGGED_IN)
        if _expired(session):
            session = _refresh(session)
    return CHAT_API_URL + ACCOUNT_RELAY, session["access_token"]


def balance():
    """Dollars left on the account. None when it can't be told (a key isn't served its balance)."""
    if api_key() or not account():
        return None
    import httpx
    import outbound
    try:
        resp = httpx.get(f"{CHAT_API_URL}/api/v1/credits/balance", headers={"Authorization": f"Bearer {credentials()[1]}"},
                         verify=outbound.trust(), timeout=10)
        return float(resp.json()["balanceUsd"]) if resp.status_code == 200 else None
    except (AuthError, httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


def forget_api_key():
    KEY_FILE.unlink(missing_ok=True)


def log_out():
    forget_api_key()
    SESSION_FILE.unlink(missing_ok=True)
