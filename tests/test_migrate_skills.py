"""The skills-ghost -> skills migration must never lose or overwrite a skill."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import migrate_skills as ms  # noqa: E402


def _skill(root, rel, name, body="body"):
    d = root / rel
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n{body}\n", encoding="utf-8")
    return d


def _home(tmp_path):
    home = tmp_path / "home"
    src = home / "skills-ghost"
    untouched = _skill(src, "research/arxiv", "arxiv")
    edited = _skill(src, "email/himalaya", "himalaya")
    (src / ".bundled_manifest").write_text(
        f"arxiv:{ms.dir_hash(untouched)}\nhimalaya:{ms.dir_hash(edited)}\n", encoding="utf-8")
    (edited / "SKILL.md").write_text("---\nname: himalaya\n---\nmy edits\n", encoding="utf-8")
    _skill(src, "red-teaming/recon", "recon")
    _skill(src, ".hub/quarantine/bad", "bad")
    _skill(src, "red-teaming/both", "both", body="old")
    _skill(home / "skills", "red-teaming/both", "both", body="mine")
    return home


def test_yours_move_bundled_stay_behind(tmp_path):
    home = _home(tmp_path)
    copied, kept = ms.migrate(home)
    assert sorted(copied) == [str(Path("email/himalaya")), str(Path("red-teaming/recon"))]
    assert kept == [str(Path("red-teaming/both"))]
    assert "my edits" in (home / "skills/email/himalaya/SKILL.md").read_text(encoding="utf-8")
    assert not (home / "skills/research/arxiv").exists()


def test_never_overwrites_and_keeps_the_original(tmp_path):
    home = _home(tmp_path)
    ms.migrate(home)
    assert "mine" in (home / "skills/red-teaming/both/SKILL.md").read_text(encoding="utf-8")
    assert "old" in (home / "skills-ghost.migrated/red-teaming/both/SKILL.md").read_text(encoding="utf-8")
    assert (home / "skills-ghost.migrated/research/arxiv/SKILL.md").exists()


def test_runs_once(tmp_path):
    home = _home(tmp_path)
    ms.migrate(home)
    assert ms.migrate(home) is None


def test_no_skills_ghost_is_a_no_op(tmp_path):
    assert ms.migrate(tmp_path) is None
    assert not (tmp_path / "skills").exists()
