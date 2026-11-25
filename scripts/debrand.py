#!/usr/bin/env python3
"""Scrub user-visible "Hermes" branding + incognito-harden a forked engine tree.

SURGICAL: replaces display strings, the two ASCII-art logo constants, the locale catalogs, and a
few targeted code patches (kill every phone-home that config alone can't reach). It never touches
Python identifiers, import paths, the `hermes_cli`/`hermes_constants` package names, `HERMES_HOME`,
config keys, or `~/.hermes` paths -- so the engine keeps running. Idempotent.

Written against the Hermes release pinned in ../engine.pin. Every code patch must land: if upstream
moves the code one targets, this exits non-zero and the install stops, because a silent miss would
leave ghost on upstream's behavior (e.g. an update check or a telemetry prompt switched back on).

    python3 debrand.py [ENGINE_DIR]   # default: ~/.ghost-engine
"""
import os, re, sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.ghost-engine")

# ghost's bridge serves the model catalogs, so nothing in the engine ever asks a third party for one.
BRIDGE = "http://127.0.0.1:8788"

# (find, replace) display phrases only -- order matters (the identity line before the general
# "Hermes Agent" rule, and the glyphs before the "👻 Hermes" label).
SUBS = [
    ("You are Hermes Agent, built by Nous Research.", "You are Ghost, a private, unrestricted AI agent."),
    ("☤ NOUS HERMES", "👻 GHOST"),
    ("Hermes Agent", "Ghost"),
    ("⚕", "👻"),
    ("☤", "👻"),
    ("I'm Hermes", "I'm Ghost"),
    ("I am Hermes", "I am Ghost"),
    ("You are Hermes", "You are Ghost"),
    ("Hermes, your", "Ghost, your"),
    ("the Hermes assistant", "the Ghost assistant"),
    ("👻 Hermes", "👻 Ghost"),
    ("Hermes CLI", "Ghost CLI"),
    ("Hermes Doctor", "Ghost Doctor"),
    ("Hermes Gateway Starting", "Ghost Gateway Starting"),
    ("chat with Hermes", "chat with Ghost"),
    ("[Hermes #", "[Ghost #"),
    # Skins compose their labels from a name + glyph.
    ('"Hermes", "👻", "Goodbye! 👻"', '"Ghost", "👻", "Goodbye! 👻"'),
    ('f"{who} Agent"', 'f"{who}"'),
    ("Welcome to {who} Agent!", "Welcome to {who}!"),
    ('"agent_name", "Hermes"', '"agent_name", "Ghost"'),
    ("agent_name='Hermes'", "agent_name='Ghost'"),
    (' or "Hermes"', ' or "Ghost"'),
    (" or 'Hermes'", " or 'Ghost'"),
    # The label after the model name in the startup banner (the model is served by OpenGradient).
    ('_dim_sep("Nous Research")', '_dim_sep("OpenGradient")'),
    ("[/] [dim {dim_color}]- Nous Research[/]", "[/] [dim {dim_color}]- OpenGradient[/]"),
    # What the model reads about its own runtime.
    ("Active Hermes profile", "Active Ghost profile"),
    ("# Hermes runtime environment", "# Ghost runtime environment"),
    ("End Hermes runtime environment", "End Ghost runtime environment"),
    # Outbound User-Agent fingerprints.
    ("HermesAgent/", "Ghost/"),
    ("Hermes-Agent/", "Ghost/"),
    ('product: str = "hermes-cli"', 'product: str = "ghost-cli"'),
    # Resume / -c hints: show the `ghost` command (not `hermes`), and drop the `-p <profile>`
    # suffix the upstream hint appends -- the ghost launcher adds the profile itself, so keeping
    # it would double the flag and break copy-paste resume. Profile-flag forms first, then general.
    ("hermes --resume {target}{profile_flag}", "ghost --resume {target}"),
    ("hermes --resume {session_id}{flag}", "ghost --resume {session_id}"),
    ("hermes --resume {self.session_id}{profile_flag}", "ghost --resume {self.session_id}"),
    ('hermes -c \\"{session_title}\\"{profile_flag}', 'ghost -c \\"{session_title}\\"'),
    ("hermes --tui --resume ", "ghost --resume "),
    ("hermes --tui -c ", "ghost -c "),
    ("hermes --resume ", "ghost --resume "),
    ("hermes -c ", "ghost -c "),
    ("(gateway.standalone: true) — temporary compatibility shim", "(gateway.standalone: true)"),
    # A refused or failed request: ghost's account is the `ghost-login` session, not an API key, and its
    # fallback is already configured.
    ("rejected your API key, so the model can't be reached. Update it in \"",
     "refused the request, so your session may have expired. Reconnect with \""),
    ("Settings → Providers, or run `hermes setup` in a terminal.", "`ghost-login` in a terminal."),
    # What a request ghost's own bridge refused looks like (the budget, an unusable reply): its reason is the news.
    ("{label} rejected this request as malformed, so the model didn't answer. Start a clean \"\n"
     "        \"session with /new or switch models with /model; if it keeps happening, run `hermes doctor`.",
     "The model didn't answer, for the reason below. If it isn't clear, start a clean session \"\n"
     "        \"with /new or switch models with /model."),
    ("{situation} To avoid this in future, \"", "{situation} \""),
    ("add a backup provider with `hermes fallback add`.\\n\\nProvider said",
     "If it keeps happening, check `ghost-login --status`.\\n\\nProvider said"),
    ("Your {provider} key was rejected. Run `hermes model` to re-enter it.",
     "Your {provider} session was refused. Run `ghost-login` to reconnect."),
    # Printed hints that the web console also matches on.
    ("Run 'hermes doctor'", "Run 'ghost doctor'"),
    ("Run 'hermes setup'", "Run 'ghost setup'"),
]

# Commands the engine tells you to run (`hermes gateway install`, ...), but only where they are display
# text -- a line that prints, or an indented example -- since elsewhere the words identify processes.
PRINTS = re.compile(r"^\s*(?:\w+\.)*(?:print|print_(?:info|warning|error|success)|cprint)\(")
COMMAND = re.compile(r"(?<![\w./~-])hermes(?= (?:-|[a-z]))")
EXAMPLE = re.compile(r"""((?:["']|\\n) {2,}(?:sudo |nohup )?)hermes(?= [a-z-])""")

# The block-letter title + the figure art (defined as `NAME = """..."""` constants in banner.py).
# Variable names kept; only the value swaps.
GHOST_LOGO = """[bold #FFD700] ██████╗ ██╗  ██╗ ██████╗ ███████╗████████╗[/]
[bold #FFD700]██╔════╝ ██║  ██║██╔═══██╗██╔════╝╚══██╔══╝[/]
[#FFBF00]██║  ███╗███████║██║   ██║███████╗   ██║   [/]
[#FFBF00]██║   ██║██╔══██║██║   ██║╚════██║   ██║   [/]
[#CD7F32]╚██████╔╝██║  ██║╚██████╔╝███████║   ██║   [/]
[#CD7F32] ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚══════╝   ╚═╝   [/]"""
GHOST_ART = """[#FFD700]    ▄████████████▄[/]
[#FFBF00]    ██████████████[/]
[#FFBF00]    ███  ████  ███[/]
[#FFBF00]    ███  ████  ███[/]
[#FFBF00]    ██████████████[/]
[#CD7F32]    ██████████████[/]
[#CD7F32]    ██████████████[/]
[#CD7F32]    ██▀██▀██▀██▀██[/]"""
ART_CONSTS = [("HERMES_AGENT_LOGO", GHOST_LOGO), ("HERMES_CADUCEUS", GHOST_ART)]

# Incognito code patches: (path, find-regex, replacement, note). @NOTE in the replacement becomes the
# comment `# ghost: <note>`, unique within its file, which is how a second run knows a patch is in.
MARK = "# ghost:"
CODE_PATCHES = [
    # -- update checks: a sealed tree never self-updates (`ghost update` does), so never ask GitHub.
    ("hermes_cli/banner.py",
     r"def prefetch_update_check\(\):\n(?:    .*\n|\n)+?    _daemon\(None, _run\)\n",
     'def prefetch_update_check():\n'
     '    """Disabled in ghost (incognito): no update-check phone-home."""\n'
     '    _update_check_done.set()  @NOTE\n',
     "no update-check phone-home"),
    ("hermes_cli/source_check.py",
     r'(def check_for_updates\((?:.|\n)+?"""(?:.|\n)+?"""\n)',
     r'\1    return {"supported": False, "behind": None, "commits": [], "reason": "managed-by-ghost",' "\n"
     '            "message": "Update with: ghost update"}  @NOTE\n',
     "no update-check phone-home"),
    # -- telemetry: never offer it, and never send it even if a config turns it on.
    ("hermes_cli/observability/shared_metrics_consent.py",
     r"(def offer_consent_if_undecided\(.*\) -> None:\n)", r"\1    return  @NOTE" "\n",
     "telemetry is never offered after setup"),
    ("hermes_cli/observability/shared_metrics_consent.py",
     r"(def offer_consent_before_chat\(.*\) -> None:\n)", r"\1    return  @NOTE" "\n",
     "telemetry is never offered before a chat"),
    ("hermes_cli/observability/shared_metrics_send_config.py",
     r"(def resolve_send_config\(.*\) -> SendConfig:\n)",
     r"\1    return SendConfig(enabled=False, send=False, endpoint=DEFAULT_ENDPOINT)  @NOTE" "\n",
     "telemetry is never sent"),
    ("hermes_cli/observability/shared_metrics_send_config.py",
     r'DEFAULT_ENDPOINT = "https://telemetry\.nousresearch\.com/v1/telemetry"',
     'DEFAULT_ENDPOINT = "http://127.0.0.1:9/disabled"  @NOTE',
     "no telemetry endpoint"),
    # -- plugins: upstream can flag a catalog plugin "default" and have it installed in every home.
    # Nothing is ever pulled into ghost that the user didn't install.
    ("hermes_cli/default_plugins.py",
     r"(def fetch_policy\(home: Path\) -> str:\n)", r'\1    return "denied"  @NOTE' "\n",
     "no remotely chosen plugin installs"),
    # -- model catalogs: answered by ghost's own bridge (which also enforces open-weight-only), with
    # no fallback to a vendor URL when the bridge is down.
    ("hermes_cli/model_catalog.py",
     r'DEFAULT_CATALOG_URL = \(\n\s+"https://[^"]+"\)',
     f'DEFAULT_CATALOG_URL = "{BRIDGE}/model-catalog.json"  @NOTE',
     "catalog served by the bridge"),
    ("hermes_cli/model_catalog.py",
     r"DEFAULT_CATALOG_FALLBACK_URLS: tuple\[str, \.\.\.\] = \(\n(?:.*\n)+?\)\n",
     "DEFAULT_CATALOG_FALLBACK_URLS: tuple[str, ...] = ()  @NOTE\n",
     "no vendor fallback"),
    ("agent/models_dev.py",
     r'MODELS_DEV_URL = "https://models\.dev/api\.json"',
     f'MODELS_DEV_URL = "{BRIDGE}/models-dev.json"  @NOTE',
     "registry served by the bridge"),
    # The gateway also re-reads OpenRouter's public model list on a timer, whatever provider you use.
    ("hermes_cli/model_catalog.py",
     r"(    catalog = get_catalog\(force_refresh=True\)\n)", r"\1    return bool(catalog)  @NOTE" "\n",
     "refresh only the bridge's catalog"),
    ("agent/model_metadata.py",
     r'(def fetch_model_metadata\(.*\n    """.*\n    global _model_metadata_cache, _model_metadata_cache_time\n)',
     r"\1    return _model_metadata_cache  @NOTE" "\n",
     "no OpenRouter lookups"),
    # -- gateway: ghost's profile keeps its own gateway on purpose (profile/config.yaml), so don't print
    # upstream's warning about it, whose suggested fix would fold the profile into a second one.
    ("hermes_cli/gateway_multiplex_mode.py",
     r'if decision\.source != "guard" or decision\.reason == SINGLE_PROFILE_REASON:',
     'if decision.source != "guard" or decision.reason in (SINGLE_PROFILE_REASON, STANDALONE_PROFILE_REASON):  @NOTE',
     "standalone on purpose"),
    ("hermes_cli/gateway_multiplex_mode.py",
     r"STANDALONE_DEPRECATION_NOTICE = \(\n(?:    .*\n)+?\)\n",
     'STANDALONE_DEPRECATION_NOTICE = "ghost keeps its own gateway for this profile"  @NOTE\n',
     "standalone by design"),
    # -- suppress the "Hermes 3/4 are NOT agentic" startup warning. ghost wires the Hermes tool-calling
    # loop to work through the OpenGradient TEE gateway (verified: native tool_calls returned, terminal
    # tool executes), so the warning is false for this configuration. This predicate gates every site.
    ("hermes_cli/model_switch.py",
     r"return bool\(model_name and _NOUS_HERMES_NON_AGENTIC_RE\.search\(model_name\)\)",
     "return False  @NOTE",
     "Hermes tool-calling verified via the OpenGradient TEE gateway"),
    # -- ghost shows the model what you see unless you turn redaction on, and its status line says which it is:
    # a red warning at every start only teaches people to ignore warnings.
    ("hermes_cli/cli_tui_runtime_mixin.py",
     r'if _redact_raw\.lower\(\) not in \{"1", "true", "yes", "on"\}:',
     "if False:  @NOTE",
     "off by default, and the launcher says when it is on"),
    # -- startup tips: skip the ones that point at accounts and closed models ghost doesn't have.
    ("hermes_cli/tips.py",
     r"    keys = tip_keys\(\)\n",
     "    import re as _re\n"
     '    _skip = _re.compile(r"nous|portal|subscription|sonnet|opus|claude|gpt-|gemini|grok|openrouter", _re.I)\n'
     "    keys = [k for k in tip_keys() if not _skip.search(t(k, lang=DEFAULT_LANGUAGE))]  @NOTE\n",
     "open-weight only"),
]

# Upstream defaults ghost flips off for EVERY profile. The shipped profile sets these too; baking them
# into the engine means a profile you create yourself, or a config.yaml that fails to parse (upstream
# then falls back to defaults), still phones nobody. Appended to config_defaults.py: a key upstream
# renamed raises KeyError at import, so the fork fails its launch check instead of drifting silently.
HARDENED_DEFAULTS = {
    "updates.check": False,                      # no passive update checks
    "plugins.auto_update_check_hours": 0,        # no background plugin update checks
    "telemetry.shared_metrics.enabled": False,
    "telemetry.shared_metrics.send": False,
    "telemetry.shared_metrics.endpoint": "http://127.0.0.1:9/disabled",
    "model_catalog.url": f"{BRIDGE}/model-catalog.json",
    "models_dev.url": f"{BRIDGE}/models-dev.json",
    "nous.guest": False,                         # never mint an anonymous vendor account
    "tools.connectors.enabled": False,           # vendor-hosted connectors (Gmail, Linear, ...)
    "auth.adopt_external_logins": False,         # never borrow Claude Code / other CLI logins for inference
    "web.keyless_fallback": False,               # never spray searches across keyless third parties
    "web.keyless_rescue": False,
    "moa.presets.default.enabled": False,        # ships pointing at OpenRouter + closed models
    "terminal.home_mode": "real",                # `~` is the user's real home, everywhere
}
HARDEN_HEADER = f"{MARK} hardened defaults (scripts/debrand.py)"
HARDEN_BLOCK = f'''

{HARDEN_HEADER}
for _path, _value in {HARDENED_DEFAULTS!r}.items():
    *_parents, _leaf = _path.split(".")
    _node = DEFAULT_CONFIG
    for _part in _parents:
        _node = _node[_part]
    if _leaf not in _node:
        raise KeyError(f"ghost: upstream no longer defines {{_path}}")
    _node[_leaf] = _value
'''

SKIP_DIRS = {"venv", ".venv", "__pycache__", "node_modules", ".git"}
EXTS = (".py", ".html", ".js", ".mjs", ".css", ".md", ".txt", ".ts", ".tsx")

# The locale catalogs hold most of the CLI's prose. Only values are display text, so the broad rules
# apply there: the product name, and `hermes <command>` hints -> `ghost <command>`.
LOCALE_SUBS = [
    (re.compile(r"\bHermes Agent\b"), "Ghost"),
    (re.compile(r"\bHermes(?! ?[34]\b)(?! 3 & 4)\b(?!-)"), "Ghost"),
    (re.compile(r"(?<![\w./~-])hermes(?= (?:-|[a-z{<]))"), "ghost"),
    (re.compile(r"`hermes`"), "`ghost`"),
]


def read(path):
    with open(path, encoding="utf-8", errors="ignore", newline="") as fh:
        return fh.read()


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def fail(lines):
    sys.stderr.write("!! debrand: code patch(es) did not apply (upstream may have changed):\n")
    for line in lines:
        sys.stderr.write(f"   - {line}\n")
    sys.exit(1)


files, subs, patches, missed = 0, 0, 0, []

# 1. code patches first, against pristine upstream text.
for rel, find, repl, note in CODE_PATCHES:
    path = os.path.join(ROOT, *rel.split("/"))
    label = f"{rel}: {find[:60]}"
    if not os.path.isfile(path):
        missed.append(f"{label}  (file gone)")
        continue
    s = read(path)
    if f"{MARK} {note}" in s:
        continue
    new, n = re.subn(find, repl.replace("@NOTE", f"{MARK} {note}"), s, count=1)
    if n:
        write(path, new)
        patches += 1
    else:
        missed.append(label)

defaults_py = os.path.join(ROOT, "hermes_cli", "config_defaults.py")
if not os.path.isfile(defaults_py):
    missed.append("hermes_cli/config_defaults.py  (file gone)")
else:
    s = read(defaults_py)
    if HARDEN_HEADER not in s:
        write(defaults_py, s.rstrip("\n") + "\n" + HARDEN_BLOCK)
        patches += 1

if missed:
    fail(missed)

# 2. display strings + art.
art_seen = set()
for dp, dirs, fs in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for f in fs:
        if not f.endswith(EXTS):
            continue
        p = os.path.join(dp, f)
        if os.path.islink(p):
            continue
        try:
            s = read(p)
        except OSError:
            continue
        orig = s
        for a, b in SUBS:
            if a in s:
                subs += s.count(a)
                s = s.replace(a, b)
        if f.endswith(".py"):
            lines = s.split("\n")
            for i, line in enumerate(lines):
                if "hermes " in line:
                    lines[i], n = COMMAND.subn("ghost", line) if PRINTS.match(line) else EXAMPLE.subn(r"\1ghost", line)
                    subs += n
            s = "\n".join(lines)
        for name, art in ART_CONSTS:
            if f == "banner.py" and name + ' = """' in s:
                s = re.sub(name + r' = """.*?"""',
                           lambda _m, n=name, a=art: f'{n} = """{a}"""', s, count=1, flags=re.DOTALL)
                art_seen.add(name)
        if s != orig:
            write(p, s)
            files += 1

# 3. locale catalogs.
locale_dir = os.path.join(ROOT, "locales")
locales = sorted(f for f in os.listdir(locale_dir) if f.endswith((".yaml", ".yml"))) if os.path.isdir(locale_dir) else []
for f in locales:
    p = os.path.join(locale_dir, f)
    s = orig = read(p)
    for a, b in SUBS:
        s = s.replace(a, b)
    for rx, b in LOCALE_SUBS:
        s, n = rx.subn(b, s)
        subs += n
    if s != orig:
        write(p, s)
        files += 1

missing_art = [name for name, _ in ART_CONSTS if name not in art_seen]
if missing_art or not locales:
    fail([f"banner art constant gone: {n}" for n in missing_art] + ([] if locales else ["locales/ is empty or gone"]))

print(f"debranded {files} files ({len(locales)} locales), {subs} display-string replacements, "
      f"{patches} code patches in {ROOT}")
