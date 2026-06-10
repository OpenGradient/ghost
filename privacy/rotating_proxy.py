#!/usr/bin/env python3
"""Local rotating forward-proxy for Hermes.

Listens on 127.0.0.1:8899. For each new connection it picks a random Webshare
upstream proxy (from webshare_proxies.txt, format ip:port:user:pass), chains the
request through it (CONNECT tunnel for HTTPS, absolute-form forward for HTTP),
and retries other upstreams if one is dead. This hides the machine's real IP
from the inference endpoint and rotates exit IPs across connections.

Run via launchd (com.advait.hermes-proxy) so it persists across sessions.
"""
import asyncio, base64, os, random, time
from urllib.parse import urlparse

PROXY_FILE = os.path.expanduser("~/.hermes/webshare_proxies.txt")
LOG_FILE   = os.path.expanduser("~/.hermes/privacy/proxy.log")
LISTEN_HOST, LISTEN_PORT = "127.0.0.1", 8899
MAX_TRIES, CONN_TIMEOUT = 4, 12
# Incognito blocklist: hosts the harness phones home to that we refuse (anonymous pricing
# lookups, telemetry, update checks). CONNECT to these returns 403 and Hermes falls back.
BLOCKLIST = {"openrouter.ai", "firecrawl-gateway.nousresearch.com"}


def load_proxies():
    out = []
    try:
        with open(PROXY_FILE) as f:
            for ln in f:
                ln = ln.strip()
                if ln and ln.count(":") >= 3:
                    h, p, u, pw = ln.split(":", 3)
                    out.append((h, int(p), u, pw))
    except FileNotFoundError:
        pass
    return out


PROXIES = load_proxies()


def log(msg):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def pick():
    return random.sample(PROXIES, min(MAX_TRIES, len(PROXIES)))


async def connect_upstream(host, port):
    """Open a CONNECT tunnel via a random upstream. Returns (r, w, ip) or None."""
    for ph, pp, pu, ppw in pick():
        try:
            ur, uw = await asyncio.wait_for(asyncio.open_connection(ph, pp), CONN_TIMEOUT)
            auth = base64.b64encode(f"{pu}:{ppw}".encode()).decode()
            uw.write((f"CONNECT {host}:{port} HTTP/1.1\r\n"
                      f"Host: {host}:{port}\r\n"
                      f"Proxy-Authorization: Basic {auth}\r\n"
                      f"Proxy-Connection: keep-alive\r\n\r\n").encode())
            await uw.drain()
            status = await asyncio.wait_for(ur.readline(), CONN_TIMEOUT)
            if b" 200 " not in status:
                uw.close()
                continue
            while True:  # drain upstream response headers
                h = await asyncio.wait_for(ur.readline(), CONN_TIMEOUT)
                if h in (b"\r\n", b"\n", b""):
                    break
            return ur, uw, ph
        except Exception:
            continue
    return None


async def handle(creader, cwriter):
    try:
        first = await asyncio.wait_for(creader.readline(), 30)
        if not first:
            cwriter.close(); return
        method, target = first.split()[0].decode("latin1"), first.split()[1].decode("latin1")
        headers = []
        while True:
            h = await asyncio.wait_for(creader.readline(), 30)
            headers.append(h)
            if h in (b"\r\n", b"\n", b""):
                break
    except Exception:
        try: cwriter.close()
        except Exception: pass
        return

    if method.upper() == "CONNECT":
        host, _, port = target.partition(":")
        port = int(port or 443)
        if any(host == b or host.endswith("." + b) for b in BLOCKLIST):
            cwriter.write(b"HTTP/1.1 403 Blocked (incognito)\r\n\r\n")
            try: await cwriter.drain()
            except Exception: pass
            cwriter.close()
            log(f"BLOCKED {host}:{port}")
            return
        up = await connect_upstream(host, port)
        if not up:
            cwriter.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            try: await cwriter.drain()
            except Exception: pass
            cwriter.close()
            log(f"FAIL CONNECT {host}:{port}")
            return
        ur, uw, ip = up
        cwriter.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        try: await cwriter.drain()
        except Exception: pass
        log(f"CONNECT {host}:{port} via {ip}")
        await asyncio.gather(pipe(creader, uw), pipe(ur, cwriter))
    else:
        u = urlparse(target)
        host, port = u.hostname, (u.port or 80)
        if not host:
            cwriter.close(); return
        for ph, pp, pu, ppw in pick():
            try:
                ur, uw = await asyncio.wait_for(asyncio.open_connection(ph, pp), CONN_TIMEOUT)
                auth = base64.b64encode(f"{pu}:{ppw}".encode()).decode()
                uw.write(first)
                uw.write(f"Proxy-Authorization: Basic {auth}\r\n".encode())
                for h in headers:
                    if not h.lower().startswith(b"proxy-"):
                        uw.write(h)
                await uw.drain()
                log(f"HTTP {host}:{port} via {ph}")
                await asyncio.gather(pipe(creader, uw), pipe(ur, cwriter))
                return
            except Exception:
                continue
        cwriter.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        cwriter.close()


async def main():
    if not PROXIES:
        log("FATAL no proxies loaded from " + PROXY_FILE)
        raise SystemExit(1)
    server = await asyncio.start_server(handle, LISTEN_HOST, LISTEN_PORT)
    log(f"listening on {LISTEN_HOST}:{LISTEN_PORT} with {len(PROXIES)} upstreams")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
