"""Tests run on people's own machines: whatever one forgets to replace must not be their ghost."""
import os
import tempfile

_home = tempfile.mkdtemp(prefix="ghost-tests-")
for name in ("GHOST_HOME", "OG_VEIL_HOME"):   # (CI points them at the ghost it has just installed)
    os.environ.setdefault(name, _home)
