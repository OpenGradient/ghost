"""ghost doctor: is everything between you and a model in order? Contacts DreamFort and the TEE registry, and no model."""
import subprocess
import sys
import time

import ghost_auth
import launch
import spend
from ghost_auth import AuthError


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
    if launch.ensure_bridge():
        return True, f"running on 127.0.0.1:{launch.PORT}", None
    return False, f"isn't running; its log is {launch.PRIV / 'bridge.log'}", "ghost update"


def relay():
    """The relay's own config document: it answers only a login it accepts, and nothing is run or billed."""
    import httpx
    import outbound
    started = time.monotonic()
    try:
        address, token = ghost_auth.credentials()
        resp = httpx.get(f"{address}/config", headers={"Authorization": f"Bearer {token}"}, timeout=20, verify=outbound.trust())
    except AuthError as e:
        return False, str(e), "ghost login"
    except httpx.HTTPError as e:
        return False, f"can't be reached ({type(e).__name__}); check your connection, VPN or proxy", None
    if resp.status_code in (401, 403):
        return False, "doesn't accept this login any more", "ghost login"
    if resp.status_code != 200:
        return False, f"isn't well ({resp.status_code}); try again in a moment", None
    return True, f"reachable, login accepted ({(time.monotonic() - started) * 1000:.0f} ms)", None


def enclaves():
    """The registry says which enclaves there are. Without it, ghost has some built in."""
    import outbound
    from enclaves import REGISTRY, Enclaves, RegistryError
    try:
        return True, f"{Enclaves(outbound.transport(REGISTRY, public_only=True)).listed()} in the registry", None
    except RegistryError:
        return None, "the registry can't be read right now, so the one built into ghost is used", None


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
    color = sys.stdout.isatty()

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
