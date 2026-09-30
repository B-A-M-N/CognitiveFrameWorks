import sys
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture(autouse=True)
def isolated_runtime_state(tmp_path):
    """Use the runtime's supported XDG state path for test isolation."""
    previous = os.environ.get("XDG_RUNTIME_DIR")
    runtime_root = tmp_path / "xdg-runtime"
    runtime_root.mkdir(mode=0o700)
    os.environ["XDG_RUNTIME_DIR"] = str(runtime_root)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("XDG_RUNTIME_DIR", None)
        else:
            os.environ["XDG_RUNTIME_DIR"] = previous
