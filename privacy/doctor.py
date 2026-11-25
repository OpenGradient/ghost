"""ghost doctor: is everything between you and a model in order? Contacts DreamFort and the TEE registry, and no model."""
import subprocess
import sys
import time

import ghost_auth
import launch
import outbound
import spend


def engine():
    binary = launch.engine_binary()
    if binary is None:
        return False, "not installed", "ghost update"
    try:
        out = subprocess.run([str(binary), "-p", "uncensored", "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    first = out.splitlines()[0] if out.strip() else ""
    return (True, first, None) if first.startswith("Ghost v") else (False, "doesn't start", "ghost update")


def login():
    label = ghost_auth.label()
    return (True, label, None) if label else (False, "nobody is logged in", "ghost login")


def bridge():
    outbound.note()
    if launch.ensure_bridge():
        return True, f"running on 127.0.0.1:{launch.PORT}", None
    return False, f"isn't running: {launch.bridge_problem()}", None if launch.port_taken() else "ghost update"


def _asked():
    """What the bridge can reach (it, and not this shell, is what your requests go through)."""
    import http.client
    import json
    conn = http.client.HTTPConnection("127.0.0.1", launch.PORT, timeout=45)
    try:
        conn.request("GET", "/check", headers={"Authorization": f"Bearer {ghost_auth.bridge_token()}"})
        return json.loads(conn.getresponse().read())
    except (OSError, ValueError, http.client.HTTPException):
        return {}
    finally:
        conn.close()


_found = {}


def relay():
    """The relay's own config document: it answers only a login it accepts, and nothing is run or billed."""
    started = time.monotonic()
    _found.clear()
    _found.update(_asked())
    if "login" in _found:
        return False, _found["login"], "ghost login"
    if "unreachable" in _found:
        script = "; this machine finds its proxy with a script, so set HTTPS_PROXY to its address" if outbound.scripted() and not outbound._noted() else ""
        return False, f"can't be reached ({_found['unreachable']}){script or '; check your connection, VPN or proxy'}", None
    status = _found.get("relay")
    if status in (401, 403):
        return False, "doesn't accept this login any more", "ghost login"
    if status != 200:
        return False, f"isn't well ({status or 'the bridge has no answer'}); try again in a moment", None
    return True, f"reachable, login accepted ({(time.monotonic() - started) * 1000:.0f} ms)", None


def enclaves():
    """The registry says which enclaves there are. Without it, ghost goes by what it said before."""
    listed = _found.get("enclaves")
    if listed is None:
        return None, "the registry can't be read right now, so one known from before is used", None
    return (True, f"{listed} in the registry", None) if listed else (False, "the registry lists none right now; try again later", None)


def credits():
    if ghost_auth.api_key():
        return True, "see https://api.dreamfort.ai", None
    left = ghost_auth.balance()
    if left is None:
        return None, "unknown", None
    return (True, f"${left:.2f} left", None) if left > 0 else (False, "none left", f"add some at {ghost_auth.CREDITS_URL}")


def spending():
    limit, hour = spend.budget(), spend.spent(spend.HOUR)
    if limit is None:
        return True, f"${hour:.2f} in the last hour, no limit set", None
    paused = spend.paused() is not None
    return not paused, f"${hour:.2f} of ${limit:.2f} in the last hour", f"ghost budget {max(limit * 2, 1):.0f}" if paused else None


def main():
    import ghost_login
    color = sys.stdout.isatty() and ghost_login.ansi()

    def style(text, code):
        return f"\x1b[{code}m{text}\x1b[0m" if color else text
    failed = False
    checks = [("Engine", engine), ("Login", login), ("Bridge", bridge)]
    if ghost_auth.mode():   # nothing is contacted before you log in
        checks += [("DreamFort", relay), ("Enclaves", enclaves), ("Credits", credits)]
    print()
    for name, check in (*checks, ("Spending", spending)):
        ok, detail, fix = check()
        failed |= ok is False
        mark = style("✗", 31) if ok is False else style("✓", 32) if ok else "·"
        print(f"  {mark} {name:<10} {detail}" + (style(f"  ->  {fix}", 33) if fix else ""), flush=True)
    print()
    return 1 if failed else 0
