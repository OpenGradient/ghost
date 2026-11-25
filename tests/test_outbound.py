"""Company networks: the system's proxy and certificates, and the one read that trusts neither."""
import os
import socket
import ssl
import sys
import threading
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "privacy"))
truststore = pytest.importorskip("truststore")
import outbound  # noqa: E402

PROXY_VARIABLES = [v for name in ("http", "https", "all", "no") for v in (f"{name}_proxy", f"{name.upper()}_PROXY")]


@pytest.fixture(autouse=True)
def no_proxy_of_this_machine(monkeypatch):
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(outbound, "NOTE", Path(os.devnull) / "none")
    monkeypatch.setattr(outbound.urllib.request, "getproxies", outbound.urllib.request.getproxies_environment)
    monkeypatch.setattr(outbound.urllib.request, "proxy_bypass", outbound.urllib.request.proxy_bypass_environment)


@pytest.mark.parametrize("env, url, expect", [
    ({}, "https://chat-api.example/x", None),
    ({"HTTPS_PROXY": "http://proxy.corp:8080"}, "https://chat-api.example/x", "http://proxy.corp:8080"),
    ({"https_proxy": "proxy.corp:8080"}, "https://chat-api.example/x", "http://proxy.corp:8080"),
    ({"ALL_PROXY": "http://proxy.corp:8080"}, "https://chat-api.example/x", "http://proxy.corp:8080"),
    ({"HTTP_PROXY": "http://proxy.corp:8080"}, "https://chat-api.example/x", None),
    ({"HTTPS_PROXY": "http://proxy.corp:8080", "NO_PROXY": ".example"}, "https://chat-api.example/x", None),
    ({"ALL_PROXY": "socks5://127.0.0.1:9050"}, "https://chat-api.example/x", "socks5://127.0.0.1:9050"),   # Tor, say
])
def test_which_proxy(monkeypatch, env, url, expect):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert outbound.proxy(url) == expect


def test_a_proxy_that_cannot_be_used_is_not_gone_around(monkeypatch):
    """That would show your address to whoever you were hiding it from."""
    monkeypatch.setenv("ALL_PROXY", "socks4://me:secret@127.0.0.1:9050")
    with pytest.raises(httpx.ProxyError, match="can't use a proxy like 127.0.0.1:9050"):
        outbound.transport("https://chat-api.example")


def test_socks_is_spoken(monkeypatch):
    pytest.importorskip("socksio")
    listener = socket.create_server(("127.0.0.1", 0))
    heard = []

    def refuse():
        conn, _ = listener.accept()
        heard.append(conn.recv(3))
        conn.sendall(b"\x05\xff")
        conn.close()
    threading.Thread(target=refuse, daemon=True).start()
    monkeypatch.setenv("ALL_PROXY", f"socks5://127.0.0.1:{listener.getsockname()[1]}")
    with httpx.Client(transport=outbound.transport("https://chat-api.example")) as client:
        with pytest.raises(httpx.ProxyError):
            client.get("https://chat-api.example/x")
    assert heard[0][:1] == b"\x05"


def test_the_bridge_is_told_what_your_shell_sets(monkeypatch, tmp_path):
    """It's a background service: launchd and systemd don't give it your shell's variables."""
    monkeypatch.setattr(outbound, "NOTE", tmp_path / "proxies.json")
    monkeypatch.setenv("HTTPS_PROXY", "http://me:secret@proxy.corp:8080")
    monkeypatch.setenv("NO_PROXY", "intranet.corp")
    outbound.note()
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    assert outbound.proxy("https://chat-api.example/x") == "http://me:secret@proxy.corp:8080"
    assert outbound.proxy("https://intranet.corp/x") is None
    assert os.name == "nt" or (tmp_path / "proxies.json").stat().st_mode & 0o77 == 0
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    outbound.note()   # a cron job without them is no reason to forget
    assert outbound.proxy("https://chat-api.example/x")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    outbound.note()   # but you, at home, are
    assert outbound.proxy("https://chat-api.example/x") is None


@pytest.mark.parametrize("public_only", [False, True])
def test_the_relay_and_the_registry_are_reached_through_it(monkeypatch, public_only):
    listener = socket.create_server(("127.0.0.1", 0))
    asked = []

    def refuse():
        conn, _ = listener.accept()
        asked.append(conn.recv(4096).split(b"\r\n")[0])
        conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        conn.close()
    threading.Thread(target=refuse, daemon=True).start()
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{listener.getsockname()[1]}")
    with httpx.Client(transport=outbound.transport("https://chat-api.example", public_only=public_only)) as client:
        with pytest.raises(httpx.ProxyError):
            client.get("https://chat-api.example/x")
    assert asked == [b"CONNECT chat-api.example:443 HTTP/1.1"]


def test_the_registry_read_does_not_trust_the_systems_certificates():
    """A network that inspects TLS is trusted by the system. It must not get to say which enclaves to trust."""
    def context(**options):
        return outbound.transport("https://x.example", **options)._pool._ssl_context
    assert isinstance(context(), truststore.SSLContext)
    assert type(context(public_only=True)) is ssl.SSLContext and context(public_only=True).verify_mode == ssl.CERT_REQUIRED


def test_the_bridge_reads_the_registry_that_way(monkeypatch):
    pytest.importorskip("opengradient_ohttp")
    import bridge
    made = {}
    monkeypatch.setattr(bridge, "_clients", {})
    monkeypatch.setattr(bridge.ghost_auth, "credentials", lambda refused=None: ("https://relay.example/x", "t"))
    monkeypatch.setattr(bridge.outbound, "transport", lambda url, **options: (url, options))
    sealing = httpx.MockTransport(lambda request: None)
    monkeypatch.setattr(bridge, "OHTTPTransport", lambda **options: made.update(options) or sealing)
    bridge._upstream()
    from opengradient_ohttp import DEFAULT_RPC_URL
    assert made["transport"].inner == ("https://relay.example/x", {})
    assert sealing._resolver._transport == (DEFAULT_RPC_URL, {"public_only": True}) and sealing._resolver._rpc_url == DEFAULT_RPC_URL


def test_the_sealing_takes_its_enclaves_from_there():
    """ghost sets this attribute of opengradient-ohttp's transport; a release that moves it must not go unnoticed."""
    pytest.importorskip("opengradient_ohttp")
    from opengradient_ohttp import OHTTPTransport
    from opengradient_ohttp._registry import TeeResolver
    assert type(OHTTPTransport()._resolver) is TeeResolver


def test_an_engine_reaches_the_bridge_without_the_proxy(monkeypatch):
    import launch
    assert launch.engine_proxies() == {}
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:8080")
    monkeypatch.setenv("NO_PROXY", "intranet.corp, localhost")
    env = launch.engine_proxies()
    assert env["https_proxy"] == env["HTTPS_PROXY"] == "http://proxy.corp:8080" and "http_proxy" not in env
    assert env["no_proxy"] == env["NO_PROXY"] == "intranet.corp,localhost,127.0.0.1,::1"
