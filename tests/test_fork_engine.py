"""scripts/fork-engine.sh around a stub engine: what it refuses, restores and restarts. No network."""
import os
import platform
import re
import subprocess
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


class Box:
    def __init__(self, root):
        self.home = root / "home"
        self.engine = self.home / ".ghost-engine"
        self.state = self.home / ".ghost"
        self.old = self.home / ".ghost-engine.old"
        self.restart = self.state / ".restart-gateway"
        self.home.mkdir()
        self.root, self.procs = root, []

    def run(self, *args, src=None, **env):
        env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "GHOST_PYTHON": "true", **env}
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

    def process(self, command):
        """A process that shows up in `ps` as `command`."""
        proc = subprocess.Popen(["bash", "-c", 'exec -a "$0" sleep 60', command])
        self.procs.append(proc)
        deadline = time.time() + 5
        while command not in subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout:
            assert time.time() < deadline
            time.sleep(0.05)
        return proc

    def service(self):
        rel = ("Library/LaunchAgents/ai.hermes.gateway-uncensored.plist" if platform.system() == "Darwin"
               else ".config/systemd/user/hermes-gateway-uncensored.service")
        unit = self.home / rel
        unit.parent.mkdir(parents=True)
        unit.write_text(f"{self.engine}/.hermes/bin/hermes --profile uncensored gateway run")


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
    assert "old -p uncensored gateway stop" in box.calls()
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


def test_refuses_while_ghost_is_open(box):
    assert box.run(src=box.source("one")).returncode == 0
    window = box.process(f"{box.engine}/.hermes/bin/hermes -p uncensored")
    for args in (["--check"], []):
        done = box.run(*args, src=box.source(f"two{len(args)}"))
        assert done.returncode != 0 and f"ghost is open (pid {window.pid})" in done.stderr
    assert (box.engine / "one").exists()
    assert box.run("--check", GHOST_FORCE="1").returncode == 0
    window.kill()
    window.wait()
    assert box.run("--check").returncode == 0


def test_running_gateway_is_stopped_then_restarted_on_the_new_engine(box):
    assert box.run(src=box.source("one")).returncode == 0
    box.service()
    box.process(f"{box.engine}/.hermes/bin/hermes --profile uncensored gateway run")
    assert box.run("--check").returncode == 0
    assert box.run(src=box.source("two")).returncode == 0
    assert "-p uncensored gateway stop" in box.calls() and box.restart.exists()
    assert box.run("--refresh-gateway").returncode == 0
    assert box.calls()[-1] == "-p uncensored gateway install --force --start-now --start-on-login"
    assert not box.restart.exists()


def test_stopped_gateway_stays_stopped(box):
    assert box.run(src=box.source("one")).returncode == 0
    box.service()
    assert box.run(src=box.source("two")).returncode == 0
    assert box.run("--refresh-gateway").returncode == 0
    assert box.calls()[-1] == "-p uncensored gateway install --force --no-start-now --start-on-login"


def test_no_service_means_no_gateway_install(box):
    assert box.run(src=box.source("one")).returncode == 0
    assert box.run("--refresh-gateway").returncode == 0
    assert not any("install" in call for call in box.calls())
