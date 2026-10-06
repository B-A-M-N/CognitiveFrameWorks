#!/usr/bin/env python3
"""CFW-only resolver and runtime boot checks; this suite never loads CSW."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.pop("COGNITIVE_STATEWORK_ROOT", None)
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from resolver.api import resolve
from runtime import start
from runtime_contract.statework_provider import (
    EmptyStateWorkProvider, statework_provider,
)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="cfw-no-csw-") as temporary:
        empty_root = Path(temporary) / "absent-statework-registry"
        provider = statework_provider(empty_root)
        assert isinstance(provider, EmptyStateWorkProvider)
        assert provider.load_stateworks() == []
        assert provider.eligible_routes(
            runtime_identity={}, task_context={})["eligible_routes"] == []
        bundle = resolve("cfw-standalone-qualification", "quick", (),
                         cow_root=empty_root)
        assert not bundle.stateworks
        session = start(bundle, telemetry_disabled=True)
        assert session.bundle is bundle
        assert not any(name.startswith("cognitive_statework") for name in sys.modules)
    print("CFW standalone resolver and runtime boot passed with CSW unavailable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
