"""Private out-of-tree runtime state locations."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def runtime_state_dir() -> Path:
    """Return a UID-private writable runtime state directory."""
    base_candidates = []
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        base_candidates.append(Path(xdg))
    base_candidates.append(Path(tempfile.gettempdir()))
    uid = os.getuid()
    for base in base_candidates:
        leaf = "cognitiveframeworks" if xdg and base == Path(xdg) else f"cognitiveframeworks-{uid}"
        candidate = base / leaf
        try:
            if candidate.is_symlink():
                continue
            candidate.mkdir(parents=True, mode=0o700, exist_ok=True)
            info = candidate.stat()
            if info.st_uid != uid:
                continue
            os.chmod(candidate, 0o700)
            probe = candidate / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return candidate
        except OSError:
            continue
    raise OSError("no writable runtime state directory available for cognitiveframeworks snapshots")
