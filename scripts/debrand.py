#!/usr/bin/env python3
"""Scrub user-visible "Hermes" branding from a forked engine tree.

SURGICAL: only replaces display-string phrases a user actually sees. It never touches
Python identifiers, import paths, the `hermes_cli`/`hermes_constants` package names, the
`HERMES_HOME` env var, config keys, or `~/.hermes` paths -- so the engine keeps running.
Scans every .py plus the web UI assets (html/js/css), excluding the venv and caches.
Idempotent. Run against the forked engine, not the upstream install.

    python3 debrand.py [ENGINE_DIR]   # default: ~/.ghost-engine
"""
import os, sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.ghost-engine")

# (find, replace) -- display phrases only; none collide with code identifiers:
# "Hermes Agent" has a space (≠ hermes_agent / hermes_cli); the rest are user-facing prose.
SUBS = [
    ("Hermes Agent", "Ghost"),
    ("⚕", "👻"),
    ("I'm Hermes", "I'm Ghost"),
    ("I am Hermes", "I am Ghost"),
    ("You are Hermes", "You are Ghost"),
    ("Hermes, your", "Ghost, your"),
    ("the Hermes assistant", "the Ghost assistant"),
]
SKIP_DIRS = {"venv", "__pycache__", "node_modules", ".git"}
EXTS = (".py", ".html", ".js", ".css", ".md", ".txt")

files, subs = 0, 0
for dp, dirs, fs in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for f in fs:
        if not f.endswith(EXTS):
            continue
        p = os.path.join(dp, f)
        try:
            s = open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        orig = s
        for a, b in SUBS:
            if a in s:
                subs += s.count(a)
                s = s.replace(a, b)
        if s != orig:
            open(p, "w", encoding="utf-8").write(s)
            files += 1

print(f"debranded {files} files, {subs} display-string replacements in {ROOT}")
