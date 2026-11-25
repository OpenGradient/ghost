"""How ghost's own requests leave this machine: through its proxy, if it has one, and trusting the certificates
it trusts, as a company laptop needs.

The bridge is a background service, which doesn't see what your shell sets (HTTPS_PROXY and so on), so `ghost`
leaves a note of it, and on Windows of the proxy a company's script (PAC) picks. A proxy that can't be used is an error: going around it would show your address.

With one exception. Which enclaves to trust is read from the TEE registry, so on a network that inspects TLS
that read must fail rather than believe the network: it trusts the public authorities only.
"""
import json
import os
import socket
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import certifi
import httpx
import truststore


def trust():
    """Certificates the system trusts (and the public ones, for a Linux without a store of its own)."""
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if sys.platform.startswith("linux"):
        context.load_verify_locations(certifi.where())
    return context


NOTE = Path(os.environ.get("GHOST_HOME") or Path.home() / ".ghost") / "privacy" / "proxies.json"


def scripted():
    """Does this machine find its proxy with a script (PAC)? Python doesn't follow those."""
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
                return bool(winreg.QueryValueEx(key, "AutoConfigURL")[0])
        except OSError:
            return False
    if sys.platform == "darwin":
        try:
            return "ProxyAutoConfigEnable : 1" in subprocess.run(["scutil", "--proxy"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return False
    return False


def _picked():
    """What that script picks for DreamFort. (Windows' own code can be asked.)"""
    ask = ("$u = [Uri]'https://chat-api.memchat.io/'; $p = [Net.WebRequest]::GetSystemWebProxy().GetProxy($u); "
           "if ($p -and $p.AbsoluteUri -ne $u.AbsoluteUri) { $p.AbsoluteUri }")
    try:
        found = subprocess.run(["powershell", "-NoProfile", "-Command", ask], capture_output=True, text=True, timeout=30).stdout.strip().rstrip("/")
    except (OSError, subprocess.SubprocessError):
        return {}
    return {"http": found, "https": found, "no": "127.0.0.1,localhost"} if found else {}


def note():
    """Leave your shell's proxies for the bridge (or that it has none, when you're there to see to it)."""
    found = urllib.request.getproxies_environment()
    if os.name == "nt" and not urllib.request.getproxies() and scripted():
        found = _picked()
    try:
        if any(found.get(kind) for kind in ("http", "https", "all")):
            fd = os.open(NOTE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(found, fh)
        elif sys.stdin.isatty():
            NOTE.unlink(missing_ok=True)
    except (OSError, AttributeError):
        pass


def _noted():
    try:
        found = json.loads(NOTE.read_text(encoding="utf-8"))
        return found if isinstance(found, dict) else {}
    except (OSError, ValueError):
        return {}


def proxy(url):
    """The proxy for `url`, from the environment, the system's settings or the note. None for a direct connection."""
    parts = urlsplit(url)
    try:
        if urllib.request.proxy_bypass(parts.hostname):
            return None
        found = urllib.request.getproxies()
    except OSError:
        found = {}
    if not any(found.get(kind) for kind in ("http", "https", "all")):
        found = _noted()
        if found and urllib.request.proxy_bypass_environment(parts.hostname, found):
            return None
    address = found.get(parts.scheme) or found.get("all")
    if not address:
        return None
    address = address if "://" in address else f"http://{address}"
    if urlsplit(address).scheme not in ("http", "https", "socks5", "socks5h"):
        raise httpx.ProxyError(f"ghost can't use a proxy like {address.split('@')[-1]} (http, https and socks5 ones it can)")
    return address


def _keepalive():
    """A reply can be minutes of silence, which some routers take for a dead connection, and sleep makes one."""
    options = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    for name, seconds in (("TCP_KEEPIDLE" if hasattr(socket, "TCP_KEEPIDLE") else "TCP_KEEPALIVE", 30), ("TCP_KEEPINTVL", 15), ("TCP_KEEPCNT", 4)):
        if hasattr(socket, name):
            options.append((socket.IPPROTO_TCP, getattr(socket, name), seconds))
    return options


def transport(url, *, public_only=False):
    verify = ssl.create_default_context(cafile=certifi.where()) if public_only else trust()
    return httpx.HTTPTransport(verify=verify, proxy=proxy(url), socket_options=_keepalive())

