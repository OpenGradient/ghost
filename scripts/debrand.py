#!/usr/bin/env python3
"""Scrub user-visible "Hermes" branding from a forked engine tree.

SURGICAL: only replaces display strings + the two ASCII-art logo constants. It never
touches Python identifiers, import paths, the `hermes_cli`/`hermes_constants` package
names, `HERMES_HOME`, config keys, or `~/.hermes` paths -- so the engine keeps running.
Scans every .py plus the web UI assets, excluding the venv and caches. Idempotent.

    python3 debrand.py [ENGINE_DIR]   # default: ~/.ghost-engine
"""
import os, re, sys

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

# The block-letter title + the figure art (defined as `NAME = """..."""` constants,
# duplicated across banner.py + cli.py). Variable names kept; only the value swaps.
GHOST_LOGO = """[bold #FFD700] ██████╗ ██╗  ██╗ ██████╗ ███████╗████████╗[/]
[bold #FFD700]██╔════╝ ██║  ██║██╔═══██╗██╔════╝╚══██╔══╝[/]
[#FFBF00]██║  ███╗███████║██║   ██║███████╗   ██║   [/]
[#FFBF00]██║   ██║██╔══██║██║   ██║╚════██║   ██║   [/]
[#CD7F32]╚██████╔╝██║  ██║╚██████╔╝███████║   ██║   [/]
[#CD7F32] ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚══════╝   ╚═╝   [/]"""
GHOST_ART = """[#FFD700]       ▄▄▄▄▄▄▄▄[/]
[#FFD700]     ▄████████████▄[/]
[#FFBF00]    ██████████████[/]
[#FFBF00]    ███  ████  ███[/]
[#FFBF00]    ███  ████  ███[/]
[#FFBF00]    ██████████████[/]
[#CD7F32]    ██████████████[/]
[#CD7F32]    ██████████████[/]
[#CD7F32]    ██▀██▀██▀██▀██[/]"""
ART_CONSTS = [("HERMES_AGENT_LOGO", GHOST_LOGO), ("HERMES_CADUCEUS", GHOST_ART)]

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
        for name, art in ART_CONSTS:
            if name + ' = """' in s:
                s = re.sub(name + r' = """.*?"""',
                           name + ' = """' + art + '"""', s, count=1, flags=re.DOTALL)
        if s != orig:
            open(p, "w", encoding="utf-8").write(s)
            files += 1

print(f"debranded {files} files, {subs} display-string replacements in {ROOT}")
