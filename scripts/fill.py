#!/usr/bin/env python3
"""Put this install's paths into a template, quoted for what it is (a script, a launchd plist, a systemd unit).

    fill.py FILE __NAME__=VALUE...
"""
import shlex
import sys
from pathlib import Path
from xml.sax.saxutils import escape

QUOTE = {".plist": escape, ".service": lambda value: value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")}


def fill(path, values):
    text = path.read_text(encoding="utf-8")
    for name, value in values:
        text = text.replace(name, QUOTE.get(path.suffix, shlex.quote)(value))
    return text


if __name__ == "__main__":
    sys.stdout.buffer.write(fill(Path(sys.argv[1]), [arg.split("=", 1) for arg in sys.argv[2:]]).encode("utf-8"))
