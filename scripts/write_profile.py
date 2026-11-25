#!/usr/bin/env python3
"""Write ghost's profile for the options chosen at install time (both installers use this).

This release's config.yaml and SOUL.md replace the ones there; what you had is kept beside them as `.prev`
when it differs.

    write_profile.py PROFILE_DIR --ghost-home DIR [--local-model NAME]
"""
import argparse
import os
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "profile"
SIDE_TASKS = "abliteration/deepseek-v4.1-flash-abliterated"
FALLBACK = "nous/hermes-4-405b"


def config(ghost_home, local_model=None):
    text = (SOURCE / "config.yaml").read_text(encoding="utf-8")
    text = text.replace("__GHOST_HOME__", Path(ghost_home).as_posix()).replace("__LOCAL_MODEL__", local_model or "ghost-tool:latest")
    if not local_model:   # hosted-only: nothing is served locally, so side tasks and the fallback are hosted too
        text = re.sub(r"provider: ollama-local\n(\s*)model: \S+", rf"provider: opengradient\n\1model: {SIDE_TASKS}", text)
        text = re.sub(r"(?m)^(fallback_model:\n  provider: opengradient\n  model: )\S+", rf"\g<1>{FALLBACK}", text)
        text = re.sub(r"(?m)^  ollama-local:\n(?:    .*\n)+", "", text)   # or /model would offer it
    return text


def install(path, text, keep=True):
    """Returns where the previous version went, if it differed."""
    previous = None
    if path.exists() and keep:
        if path.read_bytes() == text.encode("utf-8"):
            return None
        previous = path.with_name(path.name + ".prev")
        os.replace(path, previous)
    path.write_bytes(text.encode("utf-8"))
    return previous


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", type=Path)
    parser.add_argument("--ghost-home", required=True)
    parser.add_argument("--local-model")
    args = parser.parse_args(argv)
    args.profile.mkdir(parents=True, exist_ok=True)
    yours = (args.profile / "config.yaml").exists()   # in a new profile, a SOUL.md is only the engine's default
    for name, text in (("config.yaml", config(args.ghost_home, args.local_model)),
                       ("SOUL.md", (SOURCE / "SOUL.md").read_text(encoding="utf-8"))):
        previous = install(args.profile / name, text, keep=yours)
        if previous:
            print(f"your previous {name} is kept as {previous}")
    for cache in ("provider_models_cache.json", "models_dev_cache.json", "cache/model_catalog.json"):
        (args.profile / cache).unlink(missing_ok=True)   # what /model lists, which a new release may have changed
    env = args.profile / ".env"
    if not env.exists():
        env.write_bytes((SOURCE / ".env.example").read_bytes())


if __name__ == "__main__":
    main()
