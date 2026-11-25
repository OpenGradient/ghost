#!/usr/bin/env python3
"""Write ghost's profile for the options chosen at install time (both installers use this).

An update brings a new config.yaml and SOUL.md. What you changed stays changed: each is kept as it was shipped
(in .shipped, beside them), so your changes can be told from the release's and put back on top of the new ones
(the file as it was is then in config.yaml.before-update). A SOUL.md of yours is left alone, with the new one beside it as SOUL.md.new. Where yours can't be told apart (an
older ghost kept no copy, or a config.yaml isn't YAML any more), it is set aside as `.prev`.

    write_profile.py PROFILE_DIR --ghost-home DIR [--local-model NAME]
"""
import argparse
import io
import os
import re
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "profile"
SIDE_TASKS = "abliteration/deepseek-v4.1-flash-abliterated"
# The release's whatever you do: the engine's schema, and what connects it to the bridge.
OURS = {("_config_version",), ("providers", "opengradient", "api_key"), ("prefill_messages_file",)}
DELETED = object()


def config(ghost_home, local_model=None):
    os.environ["GHOST_HOME"] = str(ghost_home)
    sys.path.insert(0, str(SOURCE.parent / "privacy"))
    import ghost_auth
    text = (SOURCE / "config.yaml").read_text(encoding="utf-8")
    text = text.replace("__GHOST_HOME__", Path(ghost_home).as_posix()).replace("__LOCAL_MODEL__", local_model or "ghost-tool:latest")
    text = text.replace("__BRIDGE_TOKEN__", ghost_auth.bridge_token())
    if not local_model:   # hosted-only: nothing is served locally, so side tasks are hosted too, and there is no fallback
        text = re.sub(r"(?m)^(# Only with GHOST_LOCAL.*\n(?:#.*\n)*)?fallback_model:\n(?:  .*\n)+", "", text)
        text = re.sub(r"provider: ollama-local\n(\s*)model: \S+", rf"provider: opengradient\n\1model: {SIDE_TASKS}", text)
        text = re.sub(r"(?m)^  ollama-local:\n(?:    .*\n)+", "", text)   # or /model would offer it
    return text


# ── what you changed ──────────────────────────────────────────────────────────
def changes(shipped, yours, path=()):
    """(path, value) of every setting that isn't as it was shipped."""
    for key in [*shipped, *(k for k in yours if k not in shipped)]:
        here = (*path, key)
        if here in OURS:
            continue
        if key not in yours:
            yield here, DELETED
        elif key in shipped and isinstance(shipped[key], dict) and isinstance(yours[key], dict):
            yield from changes(shipped[key], yours[key], here)
        elif key not in shipped or shipped[key] != yours[key]:
            yield here, yours[key]


def put(settings, path, value):
    for key in path[:-1]:
        if not isinstance(settings.get(key), dict):
            if value is DELETED:
                return
            settings[key] = {}
        settings = settings[key]
    if value is DELETED:
        settings.pop(path[-1], None)
    else:
        settings[path[-1]] = value


def merged(shipped, yours, new):
    """This release's config.yaml with your changes on it. None when yours can't be read."""
    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError
    yaml = YAML()
    yaml.preserve_quotes, yaml.width = True, 120
    yaml.indent(mapping=2, sequence=4, offset=2)
    try:
        before, mine, after = (yaml.load(text) for text in (shipped, yours, new))
    except YAMLError:
        return None
    if not isinstance(mine, dict) or not isinstance(before, dict):
        return None
    found = list(changes(before, mine))
    for path, value in found:
        put(after, path, value)
    if after == mine:   # nothing new: as you laid it out, with your comments
        return yours
    if not found:
        return new
    out = io.StringIO()
    yaml.dump(after, out)
    return out.getvalue()


# ── writing ───────────────────────────────────────────────────────────────────
def write(path, text):
    """In one move, readable by you only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(text.encode("utf-8"))
    os.replace(tmp, path)


def read(path):
    try:
        return path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    except (OSError, UnicodeDecodeError):
        return None


def set_aside(path):
    previous, n = path.with_name(path.name + ".prev"), 1
    while previous.exists():   # an earlier one may be all there is of something
        n += 1
        previous = path.with_name(f"{path.name}.prev-{n}")
    os.replace(path, previous)
    print(f"your previous {path.name} is kept as {previous}")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", type=Path)
    parser.add_argument("--ghost-home", required=True)
    parser.add_argument("--local-model")
    args = parser.parse_args(argv)
    args.profile.mkdir(parents=True, exist_ok=True)
    settings, soul, kept = args.profile / "config.yaml", args.profile / "SOUL.md", args.profile / ".shipped"
    had_settings = settings.exists()   # in a new profile, a SOUL.md is only the engine's default

    new, yours, shipped = config(args.ghost_home, args.local_model), read(settings), read(kept / "config.yaml")
    final = new
    if yours is not None and yours != new:
        final = merged(shipped, yours, new) if shipped is not None else None
        if final is None:
            set_aside(settings)
            final = new
        elif final != yours and yours != shipped:
            write(settings.with_name("config.yaml.before-update"), yours)   # comments of yours, say
    elif settings.exists() and yours is None:
        set_aside(settings)
    if final != yours:
        write(settings, final)
    write(kept / "config.yaml", new)

    new, yours, shipped = (SOURCE / "SOUL.md").read_text(encoding="utf-8"), read(soul), read(kept / "SOUL.md")
    if yours is None or yours == new or yours == shipped or not had_settings:
        if yours != new:
            write(soul, new)
        soul.with_name("SOUL.md.new").unlink(missing_ok=True)
    elif shipped is None:
        set_aside(soul)
        write(soul, new)
    elif shipped != new:
        write(soul.with_name("SOUL.md.new"), new)
        print(f"your own SOUL.md is kept; this release's is beside it as {soul.with_name('SOUL.md.new')}")
    write(kept / "SOUL.md", new)

    for cache in ("provider_models_cache.json", "models_dev_cache.json", "cache/model_catalog.json"):
        (args.profile / cache).unlink(missing_ok=True)   # what /model lists, which a new release may have changed
    env = args.profile / ".env"
    if not env.exists():
        write(env, (SOURCE / ".env.example").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
