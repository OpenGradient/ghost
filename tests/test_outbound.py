"""Company networks: the system's proxy and certificates, and the one read that trusts neither."""
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
    monkeypatch.setattr(outbound.urllib.request, "getproxies", outbound.urllib.request.getproxies_environment)
    monkeypatch.setattr(outbound.urllib.request, "proxy_bypass", outbound.urllib.request.proxy_bypass_environment)


@pytest.mark.parametrize("env, url, expect", [
    ({}, "https://chat-api.example/x", None),
    ({"HTTPS_PROXY": "http://proxy.corp:8080"}, "https://chat-api.example/x", "http://proxy.corp:8080"),
    ({"https_proxy": "proxy.corp:8080"}, "https://chat-api.example/x", "http://proxy.corp:8080"),
    ({"ALL_PROXY": "http://proxy.corp:8080"}, "https://chat-api.example/x", "http://proxy.corp:8080"),
    ({"HTTP_PROXY": "http://proxy.corp:8080"}, "https://chat-api.example/x", None),
    ({"HTTPS_PROXY": "http://proxy.corp:8080", "NO_PROXY": ".example"}, "https://chat-api.example/x", None),
    ({"HTTPS_PROXY": "socks5://proxy.corp:1080"}, "https://chat-api.example/x", None),
])
def test_which_proxy(monkeypatch, env, url, expect):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert outbound.proxy(url) == expect


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
    monkeypatch.setattr(bridge.ghost_auth, "credentials", lambda: ("https://relay.example/x", "t"))
    monkeypatch.setattr(bridge.outbound, "transport", lambda url, **options: (url, options))
    monkeypatch.setattr(bridge, "OHTTPTransport", lambda **options: made.update(options) or httpx.MockTransport(lambda r: None))
    bridge._upstream()
    assert made["transport"] == ("https://relay.example/x", {}) and made["registry_transport"][1] == {"public_only": True}
    from opengradient_ohttp._registry import DEFAULT_RPC_URL
    assert made["registry_transport"][0] == DEFAULT_RPC_URL and "registry_rpc_url" not in made


def test_an_engine_reaches_the_bridge_without_the_proxy(monkeypatch):
    import launch
    assert launch.engine_proxies() == {}
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:8080")
    monkeypatch.setenv("NO_PROXY", "intranet.corp, localhost")
    env = launch.engine_proxies()
    assert env["https_proxy"] == env["HTTPS_PROXY"] == "http://proxy.corp:8080" and "http_proxy" not in env
    assert env["no_proxy"] == env["NO_PROXY"] == "intranet.corp,localhost,127.0.0.1,::1"
