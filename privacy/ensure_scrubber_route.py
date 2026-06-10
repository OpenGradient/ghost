#!/usr/bin/env python3
"""Idempotently force the uncensored profile's nous inference_base_url to the local
PII scrubber. The OAuth token-refresh path can re-validate and overwrite that field
back to the real Nous URL (silently routing around the scrubber and leaking PII), so
the hermes-uncensored wrapper runs this before every launch to self-heal."""
import json, os

AUTH = os.path.expanduser("~/.ghost/profiles/uncensored/auth.json")
SCRUBBER = "http://127.0.0.1:8788/v1"

try:
    data = json.load(open(AUTH))
except Exception:
    raise SystemExit(0)

changed = 0


def walk(o):
    global changed
    if isinstance(o, dict):
        for k in list(o):
            if k == "inference_base_url" and o[k] != SCRUBBER:
                o[k] = SCRUBBER
                changed += 1
            else:
                walk(o[k])
    elif isinstance(o, list):
        for x in o:
            walk(x)


walk(data)
if changed:
    open(AUTH, "w").write(json.dumps(data, indent=2))
