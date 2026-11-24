#!/usr/bin/env python3
"""Bring skills an older ghost kept in `skills-ghost` into `skills`, where the engine looks now.

Older ghost engines were patched to keep skills in `skills-ghost`, from when ghost shared ~/.hermes
with a normie install. ghost has had its own home since, and the engine reads `skills` everywhere, so
the patch is gone. Skills you wrote, installed, or edited are copied across; untouched bundled ones are
left out (the engine seeds its own). Nothing is overwritten, and the old directory is kept, renamed to
`skills-ghost.migrated`.

    python3 migrate_skills.py HOME_DIR [HOME_DIR ...]
"""
import hashlib
import os
import re
import shutil
import sys
from pathlib import Path


def bundled(src):
    """{skill name: origin hash} of the skills the old engine seeded."""
    out = {}
    try:
        for line in (src / ".bundled_manifest").read_text(encoding="utf-8").splitlines():
            name, _, digest = line.strip().partition(":")
            if name:
                out[name.strip()] = digest.strip()
    except OSError:
        pass
    return out


def dir_hash(directory):
    """The old engine's own content hash (tools/skills_sync.py), so an untouched copy compares equal."""
    hasher = hashlib.md5()
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            hasher.update(str(path.relative_to(directory)).encode("utf-8"))
            hasher.update(path.read_bytes())
    return hasher.hexdigest()


def skill_name(directory):
    try:
        head = (directory / "SKILL.md").read_text(encoding="utf-8", errors="ignore")[:4000]
    except OSError:
        return directory.name
    match = re.search(r"(?m)^name:\s*['\"]?([^'\"\n]+)", head)
    return match.group(1).strip() if match else directory.name


def skill_dirs(src):
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        if "SKILL.md" in files:
            dirs[:] = []   # a skill's own subdirectories belong to it
            yield Path(root)


def migrate(home):
    """Returns (copied, kept) relative paths, or None when there is nothing to migrate."""
    src, dst = home / "skills-ghost", home / "skills"
    if not src.is_dir():
        return None
    seeded, copied, kept = bundled(src), [], []
    for skill in skill_dirs(src):
        rel = skill.relative_to(src)
        origin = seeded.get(skill_name(skill)) or seeded.get(skill.name)
        if origin and origin == dir_hash(skill):
            continue
        if (dst / rel).exists():
            kept.append(str(rel))
            continue
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(skill, dst / rel, symlinks=True)
        copied.append(str(rel))
    done = home / "skills-ghost.migrated"
    n = 1
    while done.exists():
        n += 1
        done = home / f"skills-ghost.migrated-{n}"
    src.rename(done)
    return copied, kept


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        result = migrate(Path(arg).expanduser())
        if result is None:
            continue
        copied, kept = result
        print(f"   skills: moved {len(copied)} of yours from skills-ghost to skills in {arg}"
              + (f"; left {len(kept)} alone (skills/ already has them)" if kept else ""))
