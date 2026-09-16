#!/usr/bin/env python3
"""Deterministic SISPIS calibration — software, not model arithmetic.

Implements the review's cause_id merge contract:

    group by cause_id
    dedupe exact signal_type duplicates within a cause
    per entropy dimension: max effect among signals in the cause
    intent weight: max effect among signals in the cause
    output floor: strongest floor among signals in the cause
    then sum across independent causes

The model never needs a calibration table in context for this step; this
module computes E, W, and the response mode deterministically.
"""
from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Iterable

import jsonschema

ROOT = Path(__file__).resolve().parents[2]
CALIBRATION_PATH = ROOT / "SISPIS" / "references" / "signal-calibration.yaml"
SIGNAL_SCHEMA_PATH = ROOT / "shared" / "signal.schema.json"
SIGNAL_REGISTRY_PATH = ROOT / "shared" / "signal-registry.json"


def default_calibration() -> Dict[str, Any]:
    """SISPIS-owned calibration table (the single source of truth)."""
    import yaml
    if CALIBRATION_PATH.exists():
        data = yaml.safe_load(CALIBRATION_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("signals"), dict):
            return data
    return {"signals": {}, "required_actions": {}}


class SignalError(ValueError):
    pass


def _load_calibration(calibration: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return calibration if calibration is not None else default_calibration()


def _signal_registry(registry_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if registry_data is not None:
        return {item["id"]: item for item in registry_data.get("signals", [])}
    if not SIGNAL_REGISTRY_PATH.exists():
        return {}
    data = json.loads(SIGNAL_REGISTRY_PATH.read_text(encoding="utf-8"))
    return {item["id"]: item for item in data.get("signals", [])}


def _validate_envelope(envelope: Dict[str, Any], *,
                       signal_schema: Optional[Dict[str, Any]] = None,
                       signal_registry: Optional[Dict[str, Any]] = None) -> None:
    try:
        schema = signal_schema or json.loads(SIGNAL_SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()).validate(envelope)
    except jsonschema.ValidationError as exc:
        raise SignalError(f"invalid signal envelope: {exc.message}") from exc
    registry = _signal_registry(signal_registry)
    signal_type = envelope.get("signal_type")
    if signal_type not in registry:
        raise SignalError(f"unregistered signal type {signal_type!r}")
    if not envelope.get("evidence_refs"):
        raise SignalError("signal requires typed evidence references")
    if registry[signal_type].get("producer") != envelope.get("source"):
        raise SignalError(
            f"source/namespace mismatch for {signal_type!r}: "
            f"source={envelope.get('source')!r} producer={registry[signal_type].get('producer')!r}")


# entropy dimensions the calibration table maps
DIMENSIONS = ("ambiguity_of_framing", "tradeoff_density", "option_multiplicity",
              "comparative_intent", "downstream_impact")

_MODE_RANK = {"auto": 0, "direct": 1, "explanation": 2, "schema": 3}


def _strongest_floor(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if a is None:
        return b
    if b is None:
        return a
    return a if _MODE_RANK.get(a, 0) >= _MODE_RANK.get(b, 0) else b


def merge_by_cause(signals: Iterable[Dict[str, Any]], *,
                   calibration: Optional[Dict[str, Any]] = None,
                   signal_schema: Optional[Dict[str, Any]] = None,
                   signal_registry: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Merge envelopes by cause_id per the review contract.

    Each input signal is an envelope dict with at least signal_type,
    cause_id, required_action, severity. Unknown signal types are skipped
    (they contribute nothing). Returns one merged envelope per cause."""
    calibration = _load_calibration(calibration)
    table = calibration.get("signals", {})
    by_cause: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for sig in signals:
        if not isinstance(sig, dict):
            raise SignalError("signal must be an object")
        cause = sig.get("cause_id")
        if not cause:
            raise SignalError("signal requires cause_id")
        _validate_envelope(sig, signal_schema=signal_schema,
                           signal_registry=signal_registry)
        by_cause[cause].append(sig)

    merged: List[Dict[str, Any]] = []
    for cause, cause_signals in sorted(by_cause.items()):
        # dedupe exact signal_type duplicates within the cause
        seen_types: set = set()
        unique: List[Dict[str, Any]] = []
        for sig in cause_signals:
            st = sig.get("signal_type")
            if st in seen_types:
                prior = next(item for item in unique if item.get("signal_type") == st)
                if (sig.get("required_action") != prior.get("required_action") or
                        sig.get("severity") != prior.get("severity")):
                    raise SignalError(
                        f"conflicting duplicate signal type {st!r} within cause {cause!r}")
                continue
            seen_types.add(st)
            unique.append(sig)
        ent: Dict[str, float] = {d: 0.0 for d in DIMENSIONS}
        intent = 0.0
        floor: Optional[str] = None
        max_severity = "low"
        actions: set = set()
        for sig in unique:
            entry = table.get(sig.get("signal_type"), {})
            for dim, delta in (entry.get("entropy") or {}).items():
                if dim in ent:
                    ent[dim] = max(ent[dim], float(delta))
            intent = max(intent, float(entry.get("intent_weight", 0) or 0))
            floor = _strongest_floor(floor, entry.get("minimum_mode"))
            sev = sig.get("severity")
            if sev and {"low": 0, "medium": 1, "high": 2}.get(sev, 0) > {"low": 0, "medium": 1, "high": 2}.get(max_severity, 0):
                max_severity = sev
            action = sig.get("required_action")
            if action:
                actions.add(action)
            # action floors
            ra = (calibration.get("required_actions") or {}).get(action)
            if isinstance(ra, Mapping) and ra.get("minimum_mode"):
                floor = _strongest_floor(floor, ra["minimum_mode"])
        merged.append({
            "cause_id": cause,
            "entropy": {k: round(v, 6) for k, v in ent.items()},
            "intent_weight": round(intent, 6),
            "minimum_mode": floor,
            "max_severity": max_severity,
            "required_actions": sorted(actions),
            "signal_types": sorted(seen_types),
        })
    return merged


def calibrate(signals: Iterable[Dict[str, Any]], base_entropy: float = 0.0,
              base_intent: float = 0.0, *,
              calibration: Optional[Dict[str, Any]] = None,
              signal_schema: Optional[Dict[str, Any]] = None,
              signal_registry: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Merged calibration: E/W/minimum-mode from the envelope set plus a
    base. Returns the totals the gate needs."""
    merged = merge_by_cause(signals, calibration=calibration,
                            signal_schema=signal_schema,
                            signal_registry=signal_registry)
    total_entropy = min(
        10.0,
        base_entropy + sum(min(sum(c["entropy"].values()), 2.0) for c in merged))
    total_intent = base_intent + sum(c["intent_weight"] for c in merged)
    floor = None
    for c in merged:
        floor = _strongest_floor(floor, c["minimum_mode"])
    return {
        "entropy": round(total_entropy, 6),
        "intent_weight": round(total_intent, 6),
        "minimum_mode": floor,
        "causes": merged,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        signals = data.get("signals", data) if isinstance(data, dict) else data
    else:
        signals = []
    print(json.dumps(calibrate(signals), indent=2))
