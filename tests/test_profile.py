"""An update may change what it shipped, and never what you did."""
import importlib.util
import os
import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("ruamel.yaml")
from ruamel.yaml import YAML  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def profile(tmp_path, monkeypatch, capsys):
    """The installer's step, over a copy of the release that a test can change."""
    source = tmp_path / "release"
    shutil.copytree(REPO / "profile", source)
    spec = importlib.util.spec_from_file_location("write_profile", REPO / "scripts" / "write_profile.py")
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)
    monkeypatch.setattr(writer, "SOURCE", source)
    monkeypatch.setattr(writer, "config", lambda home, local=None, text=source / "config.yaml": text.read_text(encoding="utf-8")
                        .replace("__GHOST_HOME__", "/g").replace("__LOCAL_MODEL__", "m").replace("__BRIDGE_TOKEN__", "token"))

    class Profile:
        home, release = tmp_path / "profile", source

        def install(self):
            writer.main([str(self.home), "--ghost-home", "/g"])
            return capsys.readouterr().out

        def path(self, name):
            return self.home / name

        def settings(self):
            return YAML(typ="safe").load(self.path("config.yaml").read_text(encoding="utf-8"))

        def change(self, change, name="config.yaml", where=None):
            path = (where or self.home) / name
            yaml = YAML()
            data = yaml.load(path.read_text(encoding="utf-8"))
            change(data)
            with open(path, "w", encoding="utf-8") as fh:
                yaml.dump(data, fh)   # as the engine writes it: laid out its own way
    return Profile()


def test_a_new_profile_is_the_releases(profile):
    assert profile.install() == ""
    assert profile.path("config.yaml").read_text(encoding="utf-8") == (profile.release / "config.yaml").read_text(encoding="utf-8").replace(
        "__GHOST_HOME__", "/g").replace("__LOCAL_MODEL__", "m").replace("__BRIDGE_TOKEN__", "token")
    assert profile.install() == "" and not list(profile.home.glob("*.prev*"))   # and again: nothing to say
    if os.name != "nt":
        assert all(profile.path(n).stat().st_mode & 0o77 == 0 for n in ("config.yaml", "SOUL.md", ".env", ".shipped/config.yaml"))


def test_your_settings_survive_an_update(profile):
    profile.install()

    def mine(c):
        c["mcp_servers"] = {"jira": {"command": "jira-mcp", "args": ["--read-only"]}}
        c["model"]["default"] = "deepseek/deepseek-v4-pro"
        c["sessions"]["auto_prune"] = False
        c["platform_toolsets"]["cli"].append("memory")
        del c["agent"]["personalities"]["catgirl"]

    def theirs(c):
        c["agent"]["max_turns"] = 300
        c["auxiliary"]["new_task"] = {"provider": "opengradient", "model": "x"}
        c["_config_version"] = 60
        del c["tts"]
        c["agent"]["personalities"]["pirate"] = "Yarr."
    profile.change(mine)
    profile.change(theirs, where=profile.release)
    assert profile.install() == ""
    got = profile.settings()
    assert got["mcp_servers"]["jira"] == {"command": "jira-mcp", "args": ["--read-only"]}
    assert got["model"]["default"] == "deepseek/deepseek-v4-pro" and got["sessions"]["auto_prune"] is False
    assert "memory" in got["platform_toolsets"]["cli"] and "catgirl" not in got["agent"]["personalities"]
    assert (got["agent"]["max_turns"], got["_config_version"]) == (300, 60)
    assert "new_task" in got["auxiliary"] and "tts" not in got and got["agent"]["personalities"]["pirate"] == "Yarr."
    text = profile.path("config.yaml").read_text(encoding="utf-8")
    assert "# Every side task" in text   # still explained
    for _ in range(3):   # and the ones after that
        assert profile.install() == "" and profile.settings() == got
    assert not list(profile.home.glob("*.prev*"))


def test_your_comments_stay_while_there_is_nothing_new(profile):
    profile.install()
    path = profile.path("config.yaml")
    path.write_text(path.read_text(encoding="utf-8") + "# mine\n", encoding="utf-8")
    assert profile.install() == "" and path.read_text(encoding="utf-8").endswith("# mine\n")
    profile.change(lambda c: c["agent"].update(max_turns=300), where=profile.release)
    assert profile.install() == "" and profile.settings()["agent"]["max_turns"] == 300
    assert profile.path("config.yaml.before-update").read_text(encoding="utf-8").endswith("# mine\n")


def test_where_you_and_the_release_changed_the_same_thing_it_is_yours(profile):
    profile.install()
    profile.change(lambda c: c["agent"].update(reasoning_effort="high"))
    profile.change(lambda c: c["agent"].update(reasoning_effort="medium"), where=profile.release)
    profile.install()
    assert profile.settings()["agent"]["reasoning_effort"] == "high"


def test_what_connects_it_to_the_bridge_is_always_the_releases(profile):
    profile.install()

    def mine(c):
        c["providers"]["opengradient"]["api_key"] = "stale"
        c["_config_version"] = 1
        c["prefill_messages_file"] = "/elsewhere"
    profile.change(mine)
    profile.install()
    got = profile.settings()
    assert got["providers"]["opengradient"]["api_key"] == "token" and got["_config_version"] > 1
    assert got["prefill_messages_file"] == "/g/privacy/uncensored_prefill.json"


@pytest.mark.parametrize("damage", ["model: [unclosed\n", "just words\n", "\x00\x01"])
def test_a_damaged_file_is_set_aside(profile, damage):
    profile.install()
    profile.path("config.yaml").write_text(damage, encoding="utf-8")
    assert "your previous config.yaml is kept as" in profile.install()
    assert profile.path("config.yaml.prev").read_text(encoding="utf-8") == damage and profile.settings()["model"]["provider"] == "opengradient"


def test_a_file_that_is_not_text_is_set_aside(profile):
    profile.install()
    profile.path("config.yaml").write_bytes(b"\xff\xfe\x00m")
    assert "kept as" in profile.install() and profile.settings()["model"]["provider"] == "opengradient"


def test_from_an_older_ghost_which_kept_no_copy(profile):
    """Its defaults can't be told from your changes, so both are set aside, and nothing set aside is ever replaced."""
    profile.home.mkdir()
    for n in ("", "-2"):
        for name in ("config.yaml", "SOUL.md"):
            profile.path(name).write_text(f"old: {n}\n", encoding="utf-8")
        if n:
            shutil.rmtree(profile.path(".shipped"))
        out = profile.install()
        for name in ("config.yaml", "SOUL.md"):
            assert f"{name}.prev{n}" in out and profile.path(f"{name}.prev{n}").read_text(encoding="utf-8") == f"old: {n}\n"


def test_a_soul_of_yours_is_left_alone(profile):
    profile.install()
    profile.path("SOUL.md").write_text("You are Mara's.\n", encoding="utf-8")
    assert profile.install() == ""   # this release's hasn't changed
    (profile.release / "SOUL.md").write_text("You are newer.\n", encoding="utf-8")
    assert "SOUL.md.new" in profile.install()
    assert profile.path("SOUL.md").read_text(encoding="utf-8") == "You are Mara's.\n"
    assert profile.path("SOUL.md.new").read_text(encoding="utf-8") == "You are newer.\n"


def test_a_soul_you_did_not_touch_is_updated(profile):
    profile.install()
    (profile.release / "SOUL.md").write_text("You are newer.\n", encoding="utf-8")
    assert profile.install() == "" and profile.path("SOUL.md").read_text(encoding="utf-8") == "You are newer.\n"


def test_windows_line_endings_are_not_a_change(profile):
    profile.install()
    path = profile.path("config.yaml")
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert profile.install() == "" and not list(profile.home.glob("*.prev*"))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
