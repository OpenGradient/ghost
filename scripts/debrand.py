#!/usr/bin/env python3
"""Scrub user-visible "Hermes" branding + incognito-harden a forked engine tree.

SURGICAL: replaces display strings, the two ASCII-art logo constants, and a few targeted
code patches (kill the startup update-check phone-home + the Hermes User-Agent fingerprint).
It never touches Python identifiers, import paths, the `hermes_cli`/`hermes_constants`
package names, `HERMES_HOME`, config keys, or `~/.hermes` paths -- so the engine keeps
running. Scans every .py + web UI asset, excluding the venv and caches. Idempotent.

    python3 debrand.py [ENGINE_DIR]   # default: ~/.ghost-engine
"""
import os, re, sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.ghost-engine")

# (find, replace) display phrases only -- order matters (⚕->👻 before the "👻 Hermes" label).
SUBS = [
    ("Hermes Agent", "Ghost"),
    ("⚕", "👻"),
    ("I'm Hermes", "I'm Ghost"),
    ("I am Hermes", "I am Ghost"),
    ("You are Hermes", "You are Ghost"),
    ("Hermes, your", "Ghost, your"),
    ("the Hermes assistant", "the Ghost assistant"),
    ("👻 Hermes", "👻 Ghost"),
    ("Hermes CLI", "Ghost CLI"),
    ("chat with Hermes", "chat with Ghost"),
    ("[Hermes #", "[Ghost #"),
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

# Incognito code patches: (file basename, find-regex, replace). Kill phone-homes / fingerprints.
CODE_PATCHES = [
    # disable the interactive-startup github + pypi update-check phone-home
    ("banner.py",
     r"def prefetch_update_check\(\):\n(?:    .*\n)+?    t\.start\(\)",
     'def prefetch_update_check():\n    """Disabled in ghost (incognito): no update-check phone-home."""\n    _update_check_done.set()'),
    # strip the Hermes User-Agent fingerprint sent to the inference router
    ("run_agent.py",
     r'f"HermesAgent/\{_HERMES_VERSION\}"',
     'f"Ghost/{_HERMES_VERSION}"'),
    # suppress the "Hermes 3/4 are NOT agentic" startup warning. ghost wires the
    # Hermes tool-calling loop to work through the OpenGradient TEE gateway (verified:
    # native tool_calls returned, terminal tool executes), so the upstream warning is
    # false for this configuration. This single predicate is the only gate for both
    # warning sites (cli.py print + _check_hermes_model_warning), used for nothing else.
    ("model_switch.py",
     r"return bool\(_NOUS_HERMES_NON_AGENTIC_RE\.search\(model_name\)\)",
     "return False  # ghost: Hermes tool-calling verified via the OpenGradient TEE gateway"),
]

SKIP_DIRS = {"venv", "__pycache__", "node_modules", ".git"}
EXTS = (".py", ".html", ".js", ".css", ".md", ".txt")

files, subs, patches = 0, 0, 0
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
        for fname, find, repl in CODE_PATCHES:
            if f == fname and re.search(find, s):
                s = re.sub(find, repl, s, count=1)
                patches += 1
        if s != orig:
            open(p, "w", encoding="utf-8").write(s)
            files += 1

print(f"debranded {files} files, {subs} display-string replacements, {patches} code patches in {ROOT}")
