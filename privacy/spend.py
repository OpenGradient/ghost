#!/usr/bin/env python3
"""What ghost has spent on hosted models, and the limit that stops a runaway.

The bridge records the cost the enclave reports with every reply (model and token counts only, never
content) and refuses new requests once the last hour's total passes the budget.

    ghost usage            what you've spent
    ghost budget           show the hourly limit
    ghost budget 20        change it (dollars per hour)
    ghost budget off       no limit
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

DIR = Path(os.environ.get("GHOST_HOME") or Path.home() / ".ghost") / "privacy"
LEDGER = DIR / "spend.jsonl"
BUDGET_FILE = DIR / "budget"
DEFAULT_BUDGET = 5.0
HOUR, DAY = 3600, 86400
KEEP = 31 * DAY

# Dollars per million tokens (in, out), for replies whose own cost never arrived because they broke off.
PRICES = {
    "abliterated-model-large-v2": (3.0, 5.0),
    "deepseek-v4-pro": (1.28, 2.56),
    "deepseek-v4.1-flash-abliterated": (0.3, 1.2),
    "hermes-4-405b": (1.0, 3.0),
}

_lock = threading.Lock()
_entries = None  # [(time, usd)] of the last day, loaded once


def _load(since):
    out = []
    try:
        with open(LEDGER, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    if row["t"] >= since:
                        out.append(row)
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return out


def estimate(model, tokens_in, tokens_out):
    price_in, price_out = PRICES.get(model, (3.0, 5.0))
    return (tokens_in * price_in + tokens_out * price_out) / 1e6


def record(model, tokens_in, tokens_out, usd):
    global _entries
    now = time.time()
    with _lock:
        if _entries is None:
            _entries = [(r["t"], r["usd"]) for r in _load(now - DAY)]
        _entries.append((now, usd))
        try:
            DIR.mkdir(parents=True, exist_ok=True)
            row = {"t": round(now), "model": model, "in": tokens_in, "out": tokens_out, "usd": round(usd, 6)}
            fd = os.open(LEDGER, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError:
            pass


def spent(seconds):
    global _entries
    now = time.time()
    with _lock:
        if _entries is None:
            _entries = [(r["t"], r["usd"]) for r in _load(now - DAY)]
        _entries = [e for e in _entries if e[0] >= now - DAY]
        return sum(usd for t, usd in _entries if t >= now - seconds)


def budget():
    """Dollars per hour, or None for no limit."""
    try:
        text = BUDGET_FILE.read_text(encoding="utf-8").strip().lower()
    except OSError:
        return DEFAULT_BUDGET
    if text == "off":
        return None
    try:
        return float(text)
    except ValueError:
        return DEFAULT_BUDGET


def paused():
    """Why new requests are refused right now, or None."""
    limit = budget()
    if limit is None:
        return None
    total = spent(HOUR)
    if total < limit:
        return None
    return (f"ghost paused: ${total:.2f} spent in the last hour and the limit is ${limit:.2f}. To keep going, run "
            f"`ghost budget {max(limit * 2, 1):.0f}` (or `ghost budget off`) and send your message again.")


def prune():
    """Drop old rows (called when the bridge starts)."""
    rows = _load(time.time() - KEEP)
    try:
        if LEDGER.exists():
            tmp = LEDGER.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.writelines(json.dumps(r) + "\n" for r in rows)
            os.replace(tmp, LEDGER)
    except OSError:
        pass


def main(argv):
    if argv[:1] == ["budget"]:
        if len(argv) > 1:
            value = argv[1].lower().lstrip("$")
            if value != "off":
                try:
                    if float(value) <= 0:
                        raise ValueError
                except ValueError:
                    print("usage: ghost budget [DOLLARS_PER_HOUR | off]", file=sys.stderr)
                    return 2
            DIR.mkdir(parents=True, exist_ok=True)
            BUDGET_FILE.write_text(value + "\n", encoding="utf-8")
        limit = budget()
        print("Budget: no limit" if limit is None else f"Budget: ${limit:.2f} per hour")
        return 0
    now = time.time()
    rows = _load(now - KEEP)
    for name, span in (("Last hour", HOUR), ("Last 24 hours", DAY), ("Last 7 days", 7 * DAY), ("Last 30 days", 30 * DAY)):
        part = [r for r in rows if r["t"] >= now - span]
        print(f"{name:14} ${sum(r['usd'] for r in part):8.2f}   {len(part)} replies")
    limit = budget()
    print("Budget         no limit" if limit is None else f"Budget         ${limit:8.2f}   per hour (ghost budget N)")
    import ghost_auth
    left = ghost_auth.balance()
    if left is not None:
        print(f"Credits left   ${left:8.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
