"""Runtime implementation hashes and task build-manifest construction."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from runtime_contract.actions import action_contract_hash, effective_action_registry
from runtime_contract.handlers import handler_registry_hash
from runtime_contract.policy import stable_hash as _stable_contract_hash
from runtime_contract.validators import validator_registry_hash


ROOT = Path(__file__).resolve().parents[2]


def _tree_runtime_hash(paths: Iterable[Path], *,
                       relative_to: Optional[Path] = None) -> str:
    digest = hashlib.sha256()
    for path in sorted({Path(path) for path in paths}):
        if not path.exists() or not path.is_file():
            continue
        label_root = relative_to or ROOT
        try:
            label = str(path.relative_to(label_root))
        except ValueError:
            label = str(path)
        digest.update(label.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def runtime_behavior_hash() -> str:
    """Hash CFW-owned runtime code and resolver components."""
    paths = [ROOT / "scripts" / "runtime.py",
             ROOT / "scripts" / "resolve-runtime.py",
             ROOT / "scripts" / "cognitive_runtime.py",
             ROOT / "scripts" / "digital_psychology_adapter.py",
             ROOT / "SISPIS" / "runtime" / "calibrate.py"]
    paths.extend(sorted((ROOT / "scripts" / "runtime_contract").glob("*.py")))
    paths.extend(sorted((ROOT / "scripts" / "runtime_host").glob("*.py")))
    paths.extend(sorted((ROOT / "scripts" / "resolver").glob("*.py")))
    paths.extend(sorted((ROOT / "scripts" / "runtime_session").glob("*.py")))
    return _tree_runtime_hash(paths, relative_to=ROOT)


def runtime_build_manifest(
        action_registry: Optional[Mapping[str, Any]] = None) -> dict[str, str]:
    manifest = {
        "runtime_version": str(json.loads((ROOT / "plugin.json").read_text(
            encoding="utf-8")).get("version", "unknown")),
        "runtime_behavior_hash": runtime_behavior_hash(),
        "handler_registry_hash": handler_registry_hash(),
        "validator_registry_hash": validator_registry_hash(),
        "action_contract_hash": action_contract_hash(
            effective_action_registry(action_registry)),
    }
    manifest["manifest_hash"] = _stable_contract_hash(manifest)
    return manifest
