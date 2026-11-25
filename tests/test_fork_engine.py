"""scripts/fork-engine.sh around a stub engine: what it refuses, restores and restarts. No network."""
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the Windows builder is fork-engine.ps1")

REPO = Path(__file__).resolve().parent.parent
BUILDER = REPO / "scripts" / "fork-engine.sh"
VERSION = re.search(r"(?m)^HERMES_VERSION=(\S+)$", (REPO / "engine.pin").read_text()).group(1)

# Stands in for upstream's installer: a runtime whose Python does nothing, and a launcher that logs its calls.
INSTALL = r"""#!/bin/bash
while [ $# -gt 0 ]; do case "$1" in --dir) dir=$2;; --hermes-home) home=$2;; esac; shift; done
[ ! -e "$dir/BROKEN" ] || exit 1
mkdir -p "$home/tools/python-0/bin" "$dir/.hermes/bin"
printf '#!/bin/sh\nexit 0\n' > "$home/tools/python-0/bin/python3"
printf '#!/bin/sh\necho "$@" >> "%s/calls.log"\necho "Ghost v%s (stub)"\n' "$home" "$(cat "$dir/VERSION")" > "$dir/.hermes/bin/hermes"
chmod +x "$home/tools/python-0/bin/python3" "$dir/.hermes/bin/hermes"
"""


# Stand in for the service managers, which would otherwise answer for this machine's own ghost.
SERVICE_MANAGERS = {
    "launchctl": '#!/bin/sh\n[ -e "$HOME/gateway-on" ] && echo \'"PID" = 123;\' || echo \'"LastExitStatus" = 0;\'\n',
    "systemctl": '#!/bin/sh\n[ -e "$HOME/gateway-on" ]\n',
}


class Box:
    def __init__(self, root):
        self.home = root / "home"
        self.bin = root / "bin"
        self.bin.mkdir()
        for name, text in SERVICE_MANAGERS.items():
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.engine = self.home / ".ghost-engine"
        self.state = self.home / ".ghost"
        self.old = self.home / ".ghost-engine.old"
        self.restart = self.state / ".restart-gateway"
        self.home.mkdir()
        self.root, self.procs = root, []

    def run(self, *args, src=None, **env):
        env = {"PATH": f"{self.bin}:{os.environ['PATH']}", "HOME": str(self.home), "GHOST_PYTHON": "true", **env}
        if src:
            env["HERMES_SRC"] = str(src)
        return subprocess.run(["bash", str(BUILDER), *args], env=env, capture_output=True, text=True, timeout=60)

    def source(self, name, version=VERSION, broken=False):
        src = self.root / name
        (src / "scripts").mkdir(parents=True)
        (src / "scripts" / "install.sh").write_text(INSTALL)
        (src / "VERSION").write_text(version)
        (src / name).touch()
        if broken:
            (src / "BROKEN").touch()
        return src

    def calls(self):
        log = self.state / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def process(self, shape, *args):
        """A running engine process. `path`: launched by path, past the 80 columns Linux `ps` shows by default.
        `bootstrap`: the launcher's own `python -c <several lines naming the engine> <args>`."""
        argv = [sys.executable, "-c", "import time; time.sleep(60)", "-" * 80, f"{self.engine}/.hermes/bin/hermes", *args]
        if shape == "bootstrap":
            argv = [sys.executable, "-c", f"import time\nengine = '{self.engine}'\ntime.sleep(60)\n", *args]
        proc = subprocess.Popen(argv)
        self.procs.append(proc)
        deadline = time.time() + 5
        while "time.sleep(60)" not in subprocess.run(["ps", "axww", "-o", "command=", "-p", str(proc.pid)],
                                                     capture_output=True, text=True).stdout:
            assert time.time() < deadline
            time.sleep(0.05)
        return proc

    def service(self, running, runs=None):
        if running:
            (self.home / "gateway-on").touch()
        rel = ("Library/LaunchAgents/ai.hermes.gateway-uncensored.plist" if platform.system() == "Darwin"
               else ".config/systemd/user/hermes-gateway-uncensored.service")
        unit = self.home / rel
        unit.parent.mkdir(parents=True)
        unit.write_text(runs or f"{self.engine}/.hermes/bin/hermes --profile uncensored gateway run")


@pytest.fixture
def box(tmp_path):
    box = Box(tmp_path)
    yield box
    for proc in box.procs:
        proc.kill()
        proc.wait()


def test_builds_a_sealed_engine(box):
    done = box.run(src=box.source("one"))
    assert done.returncode == 0, done.stderr
    assert '"updateMechanism": "external"' in (box.engine / "install-stamp.json").read_text()
    assert sorted(p.name for p in box.home.iterdir() if p.name.startswith(".ghost-engine")) == [".ghost-engine"]


@pytest.mark.parametrize("bad", [{"broken": True}, {"version": "9.9.9"}], ids=["runtime-fails", "wrong-version"])
def test_failed_build_restores_the_previous_engine(box, bad):
    assert box.run(src=box.source("one")).returncode == 0
    done = box.run(src=box.source("two", **bad))
    assert done.returncode != 0 and "restored the previous engine" in done.stderr
    assert (box.engine / "one").exists() and not (box.engine / "two").exists() and not box.old.exists()


@pytest.mark.parametrize("broken", [False, True], ids=["upgrades", "keeps-it-on-failure"])
def test_upgrade_from_an_older_ghost(box, broken):
    """Engines before v0.23 were a venv inside the tree; that launcher has to stop its own gateway."""
    launcher = box.engine / "venv" / "bin" / "hermes"
    launcher.parent.mkdir(parents=True)
    box.state.mkdir()
    launcher.write_text(f'#!/bin/sh\necho "old $@" >> "{box.state}/calls.log"\n')
    launcher.chmod(0o755)
    done = box.run(src=box.source("new", broken=broken))
    assert (done.returncode != 0) == broken, done.stderr
    assert "old -p uncensored gateway stop --yes" in box.calls()
    assert launcher.exists() == broken and (box.engine / "new").exists() != broken


def test_failed_first_build_leaves_nothing_half_swapped(box):
    assert box.run(src=box.source("one", broken=True)).returncode != 0
    assert not (box.home / ".ghost-engine.new").exists() and not box.old.exists()
    assert box.run(src=box.source("two")).returncode == 0


def test_killed_build_is_recovered(box):
    assert box.run(src=box.source("one")).returncode == 0
    box.engine.rename(box.old)
    (box.engine / "half").mkdir(parents=True)
    assert box.run(src=box.source("two", broken=True)).returncode != 0
    assert (box.engine / "one").exists() and not (box.engine / "half").exists()


@pytest.mark.parametrize("shape", ["path", "bootstrap"])
def test_refuses_while_ghost_is_open(box, shape):
    assert box.run(src=box.source("one")).returncode == 0
    window = box.process(shape, "-p", "uncensored")
    for args in (["--check"], []):
        done = box.run(*args, src=box.source(f"two{len(args)}"))
        assert done.returncode != 0 and f"ghost is open (pid {window.pid})" in done.stderr
    assert (box.engine / "one").exists()
    assert box.run("--check", GHOST_FORCE="1").returncode == 0
    window.kill()
    window.wait()
    assert box.run("--check").returncode == 0


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_it_is_noticed_without_ps_too(box):
    """Minimal systems (a container, say) don't have it."""
    assert box.run(src=box.source("one")).returncode == 0
    tools = box.root / "tools"
    tools.mkdir()
    for directory in ("/usr/bin", "/bin"):
        for name in os.listdir(directory):
            if name != "ps" and not os.path.lexists(tools / name):
                (tools / name).symlink_to(f"{directory}/{name}")
    assert box.run("--check", PATH=str(tools)).returncode == 0
    window = box.process("path", "-p", "uncensored")
    assert f"ghost is open (pid {window.pid})" in box.run("--check", PATH=str(tools)).stderr


@pytest.mark.parametrize("shape", ["path", "bootstrap"])
def test_gateway_does_not_count_as_open(box, shape):
    assert box.run(src=box.source("one")).returncode == 0
    box.process(shape, "--profile", "uncensored", "gateway", "run")
    assert box.run("--check").returncode == 0


def test_what_a_chat_leaves_running_does_not_count_either(box):
    """Closed a minute ago, yet `ghost update` said it was open: the gateway idles on, with helpers of the engine's."""
    assert box.run(src=box.source("one")).returncode == 0
    box.procs.append(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"{box.engine}/hermes_cli/process_supervisor.sh", "123"]))
    helper = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', '{box.engine}/tools/kernel.py']).wait()"
    box.procs.append(subprocess.Popen([sys.executable, "-c", helper, f"{box.state}/tools/python", "gateway run --idle-exit"], start_new_session=True))
    deadline = time.time() + 5
    while "tools/kernel.py" not in subprocess.run(["pgrep", "-fl", "tools/kernel.py"], capture_output=True, text=True).stdout and time.time() < deadline:
        time.sleep(0.05)
    try:
        assert box.run("--check").returncode == 0
    finally:
        os.killpg(box.procs[-1].pid, 9)


@pytest.mark.parametrize("stop", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP])
def test_a_build_that_is_stopped_restores_the_previous_engine(box, stop):
    """Ctrl-C, a shutdown, or a closed terminal window."""
    assert box.run(src=box.source("one")).returncode == 0
    slow = box.source("two")
    (slow / "scripts" / "install.sh").write_text("#!/bin/bash\ntouch \"$HOME/building\"\nsleep 30\n")
    env = {"PATH": f"{box.bin}:{os.environ['PATH']}", "HOME": str(box.home), "GHOST_PYTHON": "true", "HERMES_SRC": str(slow)}
    build = subprocess.Popen(["bash", str(BUILDER)], env=env, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while not (box.home / "building").exists():
        assert time.time() < deadline
        time.sleep(0.05)
    os.killpg(build.pid, stop)
    assert build.wait(timeout=20) == 130
    assert (box.engine / "one").exists() and not (box.engine / "two").exists() and not box.old.exists()


@pytest.fixture
def released(box):
    """As users build it: fetched at the pinned commit (a stand-in `git` clones a local tree)."""
    commit = re.search(r"(?m)^HERMES_COMMIT=(\S+)$", (REPO / "engine.pin").read_text()).group(1)
    (box.bin / "git").write_text(f'''#!/bin/sh
for last; do :; done
case "$*" in
  *clone*) echo clone >> "{box.root}/fetched"; cp -R "{box.root}/upstream" "$last" ;;
  *rev-parse*) echo {commit} ;;
  *) exec {shutil.which("git")} "$@" ;;
esac
''')
    (box.bin / "git").chmod(0o755)
    box.source("upstream")
    box.fetched = lambda: len((box.root / "fetched").read_text().split()) if (box.root / "fetched").exists() else 0
    return box


def test_an_engine_that_is_current_is_not_built_again(released):
    assert released.run().returncode == 0 and released.fetched() == 1
    done = released.run()
    assert done.returncode == 0 and "already this build" in done.stdout and released.fetched() == 1


@pytest.mark.parametrize("change", ["forced", "options", "recipe", "broken"])
def test_anything_else_is(released, change):
    assert released.run(GHOST_NO_BROWSER="1").returncode == 0
    if change == "recipe":   # engine.pin, debrand.py or the builder moved on
        (released.engine / ".ghost-recipe").write_text("another\n")
    if change == "broken":
        (released.engine / ".hermes" / "bin" / "hermes").write_text("#!/bin/sh\nexit 1\n")
    env = {} if change == "options" else {"GHOST_NO_BROWSER": "1", **({"GHOST_FORCE": "1"} if change == "forced" else {})}
    assert released.run(**env).returncode == 0 and released.fetched() == 2


def test_a_current_engine_leaves_the_gateway_alone(released):
    assert released.run().returncode == 0
    released.service(running=True)
    assert released.run("--refresh-gateway").returncode == 0   # for the build just made
    before = released.calls()
    assert released.run().returncode == 0 and released.run("--refresh-gateway").returncode == 0
    assert [c for c in released.calls()[len(before):] if c != "--version"] == []


def test_running_gateway_is_stopped_then_restarted_on_the_new_engine(box):
    assert box.run(src=box.source("one")).returncode == 0
    box.service(running=True)
    assert box.run(src=box.source("two")).returncode == 0
    assert "-p uncensored gateway stop --yes" in box.calls() and box.restart.exists()
    assert box.run("--refresh-gateway").returncode == 0
    assert box.calls()[-1] == "-p uncensored gateway install --force --start-now --start-on-login"
    assert not box.restart.exists()


def test_a_gateway_an_old_ghost_ran_from_elsewhere_is_moved_too(box):
    """On Linux it ran from ~/.hermes/hermes-agent, with ghost's state. Left alone it stayed stopped."""
    assert box.run(src=box.source("one")).returncode == 0
    box.service(running=True, runs=f"HERMES_HOME={box.state}/profiles/uncensored\n{box.home}/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main")
    assert box.run(src=box.source("two")).returncode == 0 and box.restart.exists()
    assert box.run("--refresh-gateway").returncode == 0
    assert box.calls()[-1] == "-p uncensored gateway install --force --start-now --start-on-login"


def test_someone_elses_gateway_is_not_touched(box):
    assert box.run(src=box.source("one")).returncode == 0
    box.service(running=True, runs=f"HERMES_HOME={box.home}/.hermes/profiles/uncensored\n{box.home}/.hermes/hermes-agent/venv/bin/python")
    assert box.run(src=box.source("two")).returncode == 0 and not box.restart.exists()
    assert box.run("--refresh-gateway").returncode == 0 and not [c for c in box.calls() if "gateway install" in c]


def test_gateway_is_started_again_when_the_build_fails(box):
    assert box.run(src=box.source("one")).returncode == 0
    box.service(running=True)
    assert box.run(src=box.source("two", broken=True)).returncode != 0
    assert box.calls()[-1] == "-p uncensored gateway start" and not box.restart.exists()


def test_stopped_gateway_stays_stopped(box):
    assert box.run(src=box.source("one")).returncode == 0
    box.service(running=False)
    assert box.run(src=box.source("two")).returncode == 0
    assert box.run("--refresh-gateway").returncode == 0
    assert box.calls()[-1] == "-p uncensored gateway install --force --no-start-now --start-on-login"


def test_no_service_means_no_gateway_install(box):
    assert box.run(src=box.source("one")).returncode == 0
    assert box.run("--refresh-gateway").returncode == 0
    assert not any("install" in call for call in box.calls())
