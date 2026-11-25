"""How ghost's own requests leave this machine: through its proxy, if it has one, and trusting the certificates
it trusts, as a company laptop needs.

With one exception. Which enclaves to trust is read from the TEE registry, so on a network that inspects TLS
that read must fail rather than believe the network: it trusts the public authorities only.
"""
import ssl
import sys
import urllib.request
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


def proxy(url):
    """The proxy for `url`, from the environment or the system's settings. None for a direct connection."""
    parts = urlsplit(url)
    try:
        if urllib.request.proxy_bypass(parts.hostname):
            return None
        found = urllib.request.getproxies()
    except OSError:
        return None
    address = found.get(parts.scheme) or found.get("all")
    if address and "://" not in address:
        return f"http://{address}"
    return address if address and urlsplit(address).scheme in ("http", "https") else None


def transport(url, *, public_only=False):
    verify = ssl.create_default_context(cafile=certifi.where()) if public_only else trust()
    return httpx.HTTPTransport(verify=verify, proxy=proxy(url))

