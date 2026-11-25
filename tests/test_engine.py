"""Checks against a real engine built by scripts/fork-engine.{sh,ps1}.

These are what make an engine bump safe: ghost's profile must match the pinned release's config
schema, nothing the engine runs on the side may be left unpinned, and every phone-home ghost promises
is off must actually be off in the built tree.

    GHOST_ENGINE=~/.ghost-engine GHOST_HOME=~/.ghost pytest tests/test_engine.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ENGINE = Path(os.environ.get("GHOST_ENGINE") or Path.home() / ".ghost-engine")
GHOST_HOME = Path(os.environ.get("GHOST_HOME") or Path.home() / ".ghost")
PIN = dict(re.findall(r"(?m)^(HERMES_\w+)=(\S+)$", (REPO / "engine.pin").read_text(encoding="utf-8")))
BRIDGE = "http://127.0.0.1:8788"


def _store_python():
    found = sorted(GHOST_HOME.glob("tools/python-*/bin/python3")) + sorted(GHOST_HOME.glob("tools/python-*/python.exe"))
    return found[-1] if found else None


def _stamp():
    try:
        return json.loads((ENGINE / "install-stamp.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# Skipped where the pinned engine isn't built, unless GHOST_ENGINE names one: then anything else is a failure.
pytestmark = pytest.mark.skipif(
    not os.environ.get("GHOST_ENGINE")
    and (_stamp().get("distribution") != "ghost" or _stamp().get("baseVersion") != PIN["HERMES_VERSION"]
         or _store_python() is None),
    reason="needs the pinned engine (scripts/fork-engine.sh)")


def _write_profile(home, *options):
    """The profile as the installer writes it: hosted-only by default."""
    subprocess.run([sys.executable, str(REPO / "scripts" / "write_profile.py"), str(home), "--ghost-home", str(GHOST_HOME), *options],
                   check=True, capture_output=True)
    return home / "config.yaml"


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    """Run Python inside the engine, in a throwaway profile. Returns what the code assigns to `out`."""
    root = tmp_path_factory.mktemp("ghost")
    home = root / "profiles" / "uncensored"
    home.mkdir(parents=True)
    _write_profile(home)
    # The engine resolves its runtime from the root of HERMES_HOME, so the throwaway root borrows it.
    for name in ("tools", "installs"):
        try:
            (root / name).symlink_to(GHOST_HOME / name, target_is_directory=True)
        except OSError:
            subprocess.run(["cmd", "/c", "mklink", "/J", str(root / name), str(GHOST_HOME / name)], check=True, capture_output=True)

    def run(code, home=home):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "PYTHON", "UV_"))}
        env.update(HERMES_HOME=str(home), PYTHONUTF8="1")
        script = (f"import sys, json\nsys.path.insert(0, {str(ENGINE)!r})\nimport hermes_bootstrap\n{code}\n"
                  "print('\\n@@' + json.dumps(out, default=str))")
        done = subprocess.run([str(_store_python()), "-I", "-c", script], env=env, capture_output=True, text=True,
                              encoding="utf-8", timeout=180)
        assert done.returncode == 0, done.stderr[-3000:]
        return json.loads(done.stdout.rsplit("@@", 1)[1])
    run.home = home
    return run


@pytest.fixture(scope="module")
def defaults(engine):
    return engine("from hermes_cli.config_defaults import DEFAULT_CONFIG as out")


@pytest.fixture(scope="module")
def profile(engine):
    return engine("import hermes_yaml\n"
                  f"out = hermes_yaml.safe_load(open({str(engine.home / 'config.yaml')!r}, encoding='utf-8'))")


# ── the profile matches the engine ────────────────────────────────────────────
def test_config_version_matches_engine(profile, defaults):
    assert profile["_config_version"] == defaults["_config_version"]


def test_boot_migration_leaves_the_profile_alone(engine):
    before = (engine.home / "config.yaml").read_text(encoding="utf-8")
    out = engine("from hermes_cli.post_update import step_migrate_config\nout = step_migrate_config()")
    assert out == {"ok": True, "skipped": "up-to-date"}
    assert (engine.home / "config.yaml").read_text(encoding="utf-8") == before


def test_personality_is_active(engine):
    out = engine("from hermes_cli.config import load_config\n"
                 "from hermes_cli.personality import available_personalities, render_personality_prompt\n"
                 "c = load_config()\n"
                 "out = [c['display']['personality'],"
                 " render_personality_prompt(available_personalities(c)[c['display']['personality']])]")
    assert out[0] == "uncensored" and out[1].startswith("You are Ghost")


def test_every_side_task_is_pinned(profile, defaults):
    tasks = {k for k, v in defaults["auxiliary"].items() if isinstance(v, dict) and "provider" in v}
    pinned = profile["auxiliary"]
    assert tasks - set(pinned) == set(), "unpinned side tasks could leave the enclave"
    assert set(pinned) - tasks == set(), "the engine no longer has these side tasks"
    for name, route in pinned.items():
        assert route["provider"] == "opengradient", name


def test_no_retired_config_keys(profile, defaults):
    # Keys the engine reads without shipping a default for them.
    free = {"model", "providers", "fallback_model", "agent.personalities", "agent.reasoning_effort",
            "display.tool_progress", "stt.provider", "gateway.standalone", "platform_toolsets",
            "skills.creation_nudge_interval", "telemetry.shared_metrics.offer_version", "onboarding.seen"}
    unknown = []

    def walk(node, default, path=""):
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            if here in free:
                continue
            if not isinstance(default, dict) or key not in default:
                unknown.append(here)
            elif isinstance(value, dict):
                walk(value, default[key], here)
    walk(profile, defaults)
    assert unknown == []


def test_toolsets_exist(engine, profile):
    known = engine("import toolsets\nout = sorted(toolsets.TOOLSETS)")
    assert set(profile["platform_toolsets"]["cli"]) - set(known) == set()
    assert "connections" in profile["agent"]["disabled_toolsets"]


def test_command_allowlist_patterns_exist(profile):
    source = (ENGINE / "tools" / "approval_detection.py").read_text(encoding="utf-8")
    assert [p for p in profile["command_allowlist"] if f'"{p}"' not in source] == []


def test_prefill_reaches_the_model(engine):
    out = engine("from hermes_cli.config import load_config\nout = load_config()['prefill_messages_file']")
    assert out.endswith("/.ghost/privacy/uncensored_prefill.json")


def test_no_vendor_key_of_yours_is_used(engine):
    """What the engine would act on if it found it in the environment, `ghost` takes out of it first."""
    known = engine("""
from hermes_cli.auth import PROVIDER_REGISTRY
from hermes_cli.config import OPTIONAL_ENV_VARS
names = {v for p in PROVIDER_REGISTRY.values() for v in (*p.api_key_env_vars, p.base_url_env_var) if v}
out = sorted(names | {n for n, m in OPTIONAL_ENV_VARS.items() if m.get("category") in ("provider", "tool")})
""")
    sys.path.insert(0, str(REPO / "privacy"))
    import launch
    # the agent's own git and claude; which AWS account; and nothing else
    spared = {"GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "AWS_PROFILE", "AWS_REGION"}
    assert not set(known) - launch.VENDOR_ENV - spared


def test_the_engine_without_settings_has_no_provider(engine, tmp_path):
    """Pinned in its environment too, so a damaged config.yaml leads nowhere rather than to a vendor."""
    out = engine("""
import os
os.environ["HERMES_INFERENCE_PROVIDER"] = "opengradient"
from hermes_cli import runtime_provider, config
config.load_config = lambda *a, **k: {}
try:
    out = runtime_provider.resolve_runtime_provider().get("base_url")
except Exception as e:
    out = "refused"
""")
    assert out in ("refused", None, "") or "127.0.0.1" in out


def test_a_side_task_stays_on_its_own_model(engine):
    assert engine("from agent.auxiliary_client import _try_main_agent_model_fallback as f\nout = list(f('opengradient', task='title_generation'))") == [None, None, ""]


def test_searches_ask_one_engine_at_a_time(engine):
    asked = engine("""
import sys, types
asked = []
class DDGS:
    def __init__(self, timeout=None): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def text(self, query, max_results=None, backend=None):
        asked.append(backend)
        if backend == "brave": raise RuntimeError("blocked")
        return [{"title": "t", "href": "https://x", "body": "b"}] if backend == "startpage" else []
sys.modules["ddgs"] = types.SimpleNamespace(DDGS=DDGS)
from plugins.web.ddgs.provider import _run_ddgs_search
out = [asked, len(_run_ddgs_search("q", 5))]
""")
    assert asked == [["duckduckgo", "brave", "startpage"], 1]


@pytest.mark.parametrize("command, rule", [
    ("python3 -c 'print(1)'", "script execution via -e/-c flag"), ("python3 -c 'print(1)'; chmod 777 notes.txt", "world/other-writable permissions"),
    ("rm -r build", "recursive delete"), ("ls", None),
])
def test_what_is_not_routine_about_a_command_decides(engine, profile, command, rule):
    found = engine(f"from tools.approval_detection import detect_dangerous_command as d\nout = d({command!r})[1]")
    assert found == rule
    assert (rule in profile["command_allowlist"]) == (rule == "script execution via -e/-c flag")


def test_long_chats_are_summarized_before_they_get_expensive(engine):
    """No prompt cache on the gateway: at $3/M a 500K-token chat would cost $1.50 a step."""
    out = engine("from run_agent import AIAgent\nfrom hermes_cli.runtime_provider import resolve_runtime_provider\n"
                 "r = resolve_runtime_provider(requested='opengradient')\n"
                 "a = AIAgent(model='abliteration/abliterated-model-large-v2', provider=r['provider'], base_url=r['base_url'],"
                 " api_key=r['api_key'], quiet_mode=True, skip_context_files=True, skip_memory=True)\n"
                 "out = [a.context_compressor.threshold_tokens, a.context_compressor.context_length]")
    assert out == [160000, 1048576]


def test_every_route_is_served_by_the_bridge(profile):
    bridge = (REPO / "privacy" / "bridge.py").read_text(encoding="utf-8")
    served = set(re.findall(r'(?m)^    \("([\w./-]+)", "', bridge.split("_CATALOG_MODELS = [", 1)[1].split("\n]", 1)[0]))
    routes = [profile["model"]["default"]]
    assert not profile.get("fallback_model") and not profile.get("fallback_providers")   # falling back is answering again, at your expense
    routes += [route["model"] for route in profile["auxiliary"].values()]
    assert served and set(routes) - served == set()


WINDOW = ("from run_agent import AIAgent\nfrom hermes_cli.runtime_provider import resolve_runtime_provider\n"
          "def window(model, provider):\n"
          "    r = resolve_runtime_provider(requested=provider)\n"
          "    a = AIAgent(model=model, provider=r['provider'], base_url=r['base_url'], api_key=r['api_key'],\n"
          "                quiet_mode=True, enabled_toolsets=['file'], skip_context_files=True, skip_memory=True)\n"
          "    return a.context_compressor.context_length\n")


def test_context_windows(engine, profile):
    """Hosted models keep their full window, and compression can read all of it (with a smaller window the
    engine shrinks the chat's to match)."""
    out = engine(WINDOW + f"out = [window({profile['model']['default']!r}, 'opengradient'),"
                 f" window({profile['auxiliary']['compression']['model']!r}, 'opengradient'), window('nous/hermes-4-405b', 'opengradient')]")
    assert out == [1048576, 1048576, 131072]


def test_the_engine_accepts_the_local_model(engine):
    """It wants a window of at least 64K. (Installed with GHOST_LOCAL.)"""
    home = engine.home.parent / "local"
    _write_profile(home, "--local-model", "ghost-tool:latest")
    assert engine(WINDOW + "out = window('ghost-tool:latest', 'ollama-local')", home=home) == 65536


def test_model_picker_offers_what_the_bridge_serves(engine):
    """And no local provider, unless ghost was installed with one."""
    out = engine("from hermes_cli.config import load_config\nout = sorted(load_config()['providers'])")
    assert out == ["opengradient"]


# ── no phone-home ─────────────────────────────────────────────────────────────
def test_engine_is_sealed():
    stamp = _stamp()
    assert not (ENGINE / ".git").exists()
    assert (stamp["updateMechanism"], stamp["commit"]) == ("external", PIN["HERMES_COMMIT"])


def test_no_update_check(engine):
    assert engine("from hermes_cli.source_check import check_for_updates\nout = check_for_updates(force=True)")["supported"] is False


def test_telemetry_cannot_send_even_if_switched_on(engine):
    out = engine("from hermes_cli.observability.shared_metrics_send_config import resolve_send_config as r\n"
                 "c = r({'telemetry': {'shared_metrics': {'enabled': True, 'send': True}}})\n"
                 "out = [c.enabled, c.send, c.endpoint]")
    assert out[:2] == [False, False] and out[2].startswith("http://127.0.0.1")


def test_telemetry_is_never_offered(engine):
    # A person at a terminal, on a profile that never answered: the one case upstream asks in.
    out = engine("import types\nfrom hermes_cli import config, setup\n"
                 "from hermes_cli.observability import shared_metrics_consent as c\n"
                 "calls, tty = [], types.SimpleNamespace(isatty=lambda: True)\n"
                 "c.sys = types.SimpleNamespace(stdin=tty, stdout=tty)\n"
                 "config.is_managed = setup.is_noninteractive = lambda: False\n"
                 "c._current_state = lambda: {'decided': False, 'reask': False}\n"
                 "c.offer_consent = lambda *a, **k: calls.append(1)\n"
                 "c.offer_consent_if_undecided(); c.offer_consent_before_chat(types.SimpleNamespace())\n"
                 "out = calls")
    assert out == []


def test_no_remote_default_plugins(engine):
    assert engine("from pathlib import Path\nfrom hermes_cli.default_plugins import fetch_policy\n"
                  f"out = fetch_policy(Path({str(engine.home)!r}))") == "denied"


def test_catalogs_come_from_the_bridge(engine):
    out = engine("from hermes_cli import model_catalog as m\nfrom agent import models_dev as d\n"
                 "out = [m.DEFAULT_CATALOG_URL, list(m.DEFAULT_CATALOG_FALLBACK_URLS), d.MODELS_DEV_URL]")
    assert out == [f"{BRIDGE}/model-catalog.json", [], f"{BRIDGE}/models-dev.json"]


def test_catalog_refresh_stays_on_the_bridge(engine):
    # The gateway runs these on a timer; upstream also asks OpenRouter, whatever provider you use.
    out = engine("import socket\nhosts, real = [], socket.getaddrinfo\n"
                 "def spy(host, *a, **k):\n"
                 "    hosts.append(host if isinstance(host, str) else (host or b'').decode())\n"
                 "    raise OSError('offline')\n"
                 "socket.getaddrinfo = spy\n"
                 "from hermes_cli import model_catalog\nfrom agent.model_metadata import fetch_model_metadata\n"
                 "model_catalog.refresh_catalogs(); fetch_model_metadata(force_refresh=True)\n"
                 "out = sorted(set(hosts))")
    assert out == ["127.0.0.1"]


def test_standalone_gateway_is_not_nagged_into_migrating(engine):
    out = engine("from hermes_cli import gateway_multiplex_mode as g\n"
                 "d = g.MultiplexDecision(False, 'guard', g.STANDALONE_PROFILE_REASON)\n"
                 "out = [g.standalone_warning_lines(d, ['default']), g.STANDALONE_DEPRECATION_NOTICE]")
    assert out[0] == [] and "migrate" not in out[1]


@pytest.mark.parametrize("path, value", [
    ("updates.check", False), ("plugins.auto_update_check_hours", 0), ("nous.guest", False),
    ("tools.connectors.enabled", False), ("auth.adopt_external_logins", False), ("web.keyless_fallback", False),
    ("web.keyless_rescue", False), ("moa.presets.default.enabled", False), ("terminal.home_mode", "real"),
    ("telemetry.shared_metrics.enabled", False), ("telemetry.shared_metrics.send", False),
    ("model_catalog.url", f"{BRIDGE}/model-catalog.json"), ("models_dev.url", f"{BRIDGE}/models-dev.json"),
])
def test_hardened_default(defaults, path, value):
    node = defaults
    for part in path.split("."):
        node = node[part]
    assert node == value


def test_broken_config_stays_hardened(engine, tmp_path):
    """Upstream falls back to its defaults when config.yaml doesn't parse: those must be ghost's."""
    home = engine.home.parent / "broken"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text("updates: [unclosed\n", encoding="utf-8")
    try:
        out = engine("from hermes_cli.config import load_config\nc = load_config()\n"
                     "out = [c['updates']['check'], c['nous']['guest'], c['auth']['adopt_external_logins'],"
                     " c['web']['keyless_fallback'], c['telemetry']['shared_metrics']['send']]", home=home)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    assert out == [False] * 5


# ── branding ──────────────────────────────────────────────────────────────────
def test_launcher_says_ghost():
    names = ("hermes.exe", "hermes.cmd") if os.name == "nt" else ("hermes",)
    launcher = next(p for p in (ENGINE / ".hermes" / "bin" / n for n in names) if p.exists())
    env = {**os.environ, "HERMES_HOME": str(GHOST_HOME / "profiles" / "uncensored"), "PYTHONUTF8": "1"}
    out = subprocess.run([str(launcher), "--version"], env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert out.stdout.startswith(f"Ghost v{PIN['HERMES_VERSION']}"), out.stdout + out.stderr
    assert "pdate available" not in out.stdout


def test_chat_labels_say_ghost(engine):
    out = engine("from hermes_cli.skin_engine import get_active_skin\ns = get_active_skin()\n"
                 "out = [s.get_branding(k) for k in ('agent_name', 'welcome', 'response_label', 'goodbye')]")
    assert out[0] == "Ghost" and out[1].startswith("Welcome to Ghost!") and out[2].strip() == "👻 Ghost"
    assert "Hermes" not in "".join(out)


def test_printed_commands_say_ghost():
    printed = (ENGINE / "hermes_cli" / "gateway_launchd.py").read_text(encoding="utf-8")
    assert 'print("  ghost gateway status' in printed and 'print("  hermes gateway' not in printed
    listed = (ENGINE / "hermes_cli" / "gateway.py").read_text(encoding="utf-8")
    assert '"  ghost gateway install"' in listed and '"  hermes gateway install"' not in listed


def test_locales_parse_and_are_debranded(engine):
    out = engine("import hermes_yaml, pathlib\n"
                 "def flat(n):\n"
                 "    return [x for v in n.values() for x in flat(v)] if isinstance(n, dict) else [str(n)]\n"
                 f"files = sorted(pathlib.Path({str(ENGINE / 'locales')!r}).glob('*.yaml'))\n"
                 "docs = {p.stem: flat(hermes_yaml.safe_load(p.read_text(encoding='utf-8'))) for p in files}\n"
                 "out = [len(docs), [k for k, v in docs.items() if any('\u2624' in x or 'Hermes Agent' in x for x in v)]]")
    assert out[0] > 1 and out[1] == []


def test_startup_tips_skip_closed_models(engine):
    out = engine("from hermes_cli.tips import get_random_tip\nout = [get_random_tip() for _ in range(400)]")
    assert out and not [t for t in out if re.search(r"(?i)nous|sonnet|gpt-|gemini|openrouter", t)]
