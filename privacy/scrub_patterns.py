"""Single source of truth for ghost's secret + PII regexes.

Both scrubbers import from here -- the Presidio path (presidio_scrub.py) and the legacy regex
path (scrubbing_proxy.py). Keeping the patterns in one place avoids the worst kind of drift: a
secret fix that lands in one path but still leaks through the other. Secrets are ALWAYS scrubbed
(even with PII redaction off), so coverage here is the last line of defense.
"""
import re

# ── PII (used by the legacy regex scrubber; Presidio handles these semantically) ──────────────
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CC_RE = re.compile(r"\b(?:\d[ -]*?){13,16}\b")
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d\-\s().]{7,}\d(?!\w)")

# ── Secrets (ALWAYS scrubbed) ─────────────────────────────────────────────────────────────────
SECRET_RES = [
    # OpenAI / Anthropic / Nous style: sk-, rk-, sk-ant-, sk-proj-, ...
    re.compile(r"\b(?:sk|rk)-(?:ant-|nous-|proj-|live-|test-)?[A-Za-z0-9_-]{16,}"),
    # Stripe-style underscore keys + webhook signing secrets (sk_live_, pk_test_, whsec_)
    re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bwhsec_[A-Za-z0-9]{16,}\b"),
    # GitHub classic tokens (ghp_/gho_/...) + fine-grained PATs
    re.compile(r"\bgh[posru]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    # AWS access key id
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # Google API key + GCP OAuth access token
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}"),
    # Slack bot/user/app tokens
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bxapp-[A-Za-z0-9-]{10,}\b"),
    # Fireworks, Replicate
    re.compile(r"\bfw_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\br8_[A-Za-z0-9]{20,}\b"),
    # JWTs (header.payload.signature)
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    # PEM private keys
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----[\s\S]+?-----END[^-]+-----"),
]
