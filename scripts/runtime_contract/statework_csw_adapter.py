"""CognitiveStateWork adapter implementing CFW's optional provider contract."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

import yaml

from runtime_contract.statework_provider import StateWorkProviderError


class CognitiveStateWorkProvider:
    """Bridge from a CSW checkout's compiled registry and validated primitives."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._control: Any = None
        self._compiled: Optional[Dict[str, Any]] = None

    def _control_plane(self) -> Any:
        if self._control is not None:
            return self._control
        parent = str(self.root.parent)
        if parent not in sys.path:
            sys.path.insert(0, parent)
        package = self.root.name
        try:
            module = importlib.import_module(f"{package}.scripts.control_plane")
        except Exception as exc:
            raise StateWorkProviderError(
                f"CognitiveStateWork control plane is unavailable: {exc}") from exc
        expected = (self.root / "scripts" / "control_plane.py").resolve()
        actual = Path(str(getattr(module, "__file__", ""))).resolve()
        if actual != expected:
            raise StateWorkProviderError(
                f"CognitiveStateWork control plane resolved outside configured root: {actual}")
        self._control = module
        return self._control

    def _compiled_registry(self) -> Dict[str, Any]:
        if self._compiled is None:
            try:
                data = self._control_plane().load_compiled_registry(self.root)
            except Exception as exc:
                raise StateWorkProviderError(
                    f"compiled StateWork registry is invalid: {exc}") from exc
            if not isinstance(data, Mapping):
                raise StateWorkProviderError("compiled StateWork registry is invalid")
            self._compiled = dict(data)
        return self._compiled

    def load_stateworks(self) -> list[dict[str, Any]]:
        compiled = self._compiled_registry()
        out = []
        for entry in compiled.get("stateworks") or ():
            data = dict(entry.get("manifest") or {})
            owner = Path(str(entry.get("bundle_root") or ""))
            if not owner.is_dir():
                raise StateWorkProviderError(
                    f"compiled StateWork {entry.get('id')!r} source root is missing")
            data["_manifest_path"] = owner / "manifest.yaml"
            data["_bundle_root"] = str(owner)
            data["_bundle_hash"] = entry.get("bundle_hash")
            data["_compiled_engine"] = dict(entry.get("engine") or {})
            data["_runtime_capsule"] = str(entry.get("runtime_capsule") or "")
            data["_runtime_capsule_hash"] = str(entry.get("runtime_capsule_hash") or "")
            out.append(data)
        for item in out:
            self.validate_flow_contracts(str(item.get("id") or ""),
                                          owner_root=Path(item["_bundle_root"]))
        return out

    def packet_registry(self) -> Dict[str, Any]:
        data = self._compiled_registry().get("packet_registry") or {}
        return dict(data) if isinstance(data, Mapping) else {"packets": {}}

    def packet_store(self, packets: Optional[Iterable[Dict[str, Any]]]) -> Any:
        compiled = self._compiled_registry()
        return self._control_plane().PacketStore(
            packets, packet_registry_data=compiled.get("packet_registry") or {},
            packet_schemas=compiled.get("packet_schemas") or {})

    def bundle_root(self, statework: Any) -> Path:
        if isinstance(statework, Mapping) and statework.get("_bundle_root"):
            return Path(str(statework["_bundle_root"]))
        statework_id = (str(statework.get("id") or "") if isinstance(statework, Mapping)
                        else str(statework))
        entry = next((item for item in self._compiled_registry().get("stateworks", ())
                      if item.get("id") == statework_id), None)
        if entry is None:
            raise StateWorkProviderError(
                f"compiled StateWork {statework_id!r} is unavailable")
        return Path(str(entry["bundle_root"]))

    def validate_flow_contracts(self, statework_id: str, *,
                                owner_root: Optional[Path] = None) -> None:
        flows_dir = (owner_root or self.bundle_root(statework_id)) / "flows"
        paths = sorted(flows_dir.glob("*/flow.md")) if flows_dir.is_dir() else []
        if not paths:
            return
        entry = next((item for item in self._compiled_registry().get("stateworks", ())
                      if item.get("id") == statework_id), None)
        if entry is None:
            raise StateWorkProviderError(
                f"compiled StateWork {statework_id!r} is unavailable")
        policy = dict((entry.get("manifest") or {}).get("flow_selection") or {})
        selector = self.flow_selector()
        fallbacks = []
        for path in paths:
            name = path.parent.name
            pieces = path.read_text(encoding="utf-8").split("---", 2)
            if len(pieces) != 3 or pieces[0].strip():
                raise StateWorkProviderError(
                    f"StateWork {statework_id!r} flow {name!r} lacks machine-readable frontmatter")
            try:
                meta = yaml.safe_load(pieces[1]) or {}
            except Exception as exc:
                raise StateWorkProviderError(
                    f"StateWork {statework_id!r} flow {name!r} has invalid frontmatter") from exc
            if not isinstance(meta, Mapping) or meta.get("flow") != name:
                raise StateWorkProviderError(
                    f"StateWork {statework_id!r} flow {name!r} metadata is invalid")
            if "when" in meta:
                try:
                    selector.validate_predicate(meta["when"])
                except Exception as exc:
                    raise StateWorkProviderError(
                        f"StateWork {statework_id!r} flow {name!r} selector is invalid") from exc
            elif meta.get("fallback") is not True:
                raise StateWorkProviderError(
                    f"StateWork {statework_id!r} flow {name!r} needs when or fallback=true")
            if meta.get("fallback") is True:
                fallbacks.append(name)
        fallback_policy = str(policy.get("fallback") or "")
        if ((fallback_policy == "required" and len(fallbacks) != 1)
                or (fallback_policy == "optional" and len(fallbacks) > 1)
                or (fallback_policy == "forbidden" and fallbacks)):
            raise StateWorkProviderError(
                f"StateWork {statework_id!r} fallback policy {fallback_policy!r} "
                f"does not match declared flows {fallbacks}")

    def flow_metadata(self, statework: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
        statework_id = str(statework.get("id") or "")
        owner_root = self.bundle_root(statework)
        self.validate_flow_contracts(statework_id, owner_root=owner_root)
        flows_dir = owner_root / "flows"
        result: Dict[str, Dict[str, Any]] = {}
        for path in sorted(flows_dir.glob("*/flow.md")) if flows_dir.is_dir() else ():
            name = path.parent.name
            try:
                pieces = path.read_text(encoding="utf-8").split("---", 2)
                meta = yaml.safe_load(pieces[1]) or {}
            except Exception as exc:
                raise StateWorkProviderError(
                    f"StateWork {statework_id!r} flow {name!r} has invalid frontmatter") from exc
            if not isinstance(meta, Mapping) or meta.get("flow") != name:
                raise StateWorkProviderError(
                    f"StateWork {statework_id!r} flow {name!r} metadata is invalid")
            result[name] = dict(meta)
        return result

    def flow_selector(self) -> Any:
        return self._control_plane().FlowSelector

    def runtime_hash_paths(self) -> list[Path]:
        paths = [self.root / "scripts" / "control_plane.py",
                 self.root / "scripts" / "workflow_engines.py",
                 self.root / "scripts" / "flow_selectors.py",
                 self.root / "compiled-registry.json",
                 self.root / "registry.yaml"]
        paths.extend((self.root / "schemas").glob("*.json"))
        paths.extend((self.root / "schemas").glob("*.yaml"))
        return paths

    def behavior_hash_paths(self) -> list[Path]:
        if not (self.root / "registry.yaml").is_file():
            return []
        return [self.root / "scripts" / "control_plane.py",
                self.root / "scripts" / "workflow_engines.py",
                self.root / "scripts" / "flow_selectors.py"]

    def registry_document(self) -> Dict[str, Any]:
        try:
            data = yaml.safe_load((self.root / "registry.yaml").read_text(encoding="utf-8"))
        except Exception as exc:
            raise StateWorkProviderError(f"StateWork registry is invalid: {exc}") from exc
        if not isinstance(data, Mapping):
            raise StateWorkProviderError("StateWork registry is invalid")
        return dict(data)
