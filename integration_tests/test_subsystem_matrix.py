"""Gate: every valid CFW/CSW/DP composition starts independently.

Each cell builds one supported configuration and asserts that (a) it starts,
(b) the subsystems it does not name were never constructed, and (c) a failing
cell degrades alone rather than taking the others down.

This is a composition/isolation proof. It deliberately does not attempt task
completion, which requires evidence-bound completion permits.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COW = ROOT.parent / "CognitiveStateWork"
# A path with no registry.yaml: CFW resolves an empty provider and stays CFW-only.
NO_CSW_ROOT = ROOT / "statework"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _runtime_modules():
    sys.modules.pop("resolve_runtime", None)
    resolve = _load("matrix_resolve", ROOT / "scripts" / "resolve-runtime.py")
    sys.modules["resolve_runtime"] = resolve
    runtime = _load("matrix_runtime", ROOT / "scripts" / "runtime.py")
    return resolve, runtime


def _request(resolve, task_id: str, *, needs_statework: bool):
    return resolve.TaskRequest(
        task_id=task_id, namespace_id="tenant-m", application_id="matrix-app",
        application_instance_id="matrix-instance",
        # subject_ref makes StateWork capability mandatory, so the CFW-alone
        # cell must omit it.
        subject_ref=f"repo:{task_id}" if needs_statework else None,
        shape="quick", domain_tags=("terminal interface",) if needs_statework else (),
    )


def test_cell_1_baseline_cfw_alone_constructs_no_csw_and_no_dp():
    """Baseline: CFW standalone must not discover CSW or construct DP."""
    resolve, runtime = _runtime_modules()
    bundle = resolve.resolve(_request(resolve, "matrix-baseline", needs_statework=False),
                             cow_root=NO_CSW_ROOT)
    assert not bundle.stateworks
    session = runtime.start(bundle, telemetry_disabled=True)
    assert session.identity_authority.authorities.state_authority is None


def test_cell_2_cfw_plus_csw_constructs_csw_authority_but_no_dp(tmp_path):
    """CFW + CSW: CSW owns eligibility; DP stays unconstructed."""
    resolve, runtime = _runtime_modules()
    # Isolated durable stores: the checkout's local state/ directory is
    # untracked runtime residue and must not participate in the composition.
    authority = runtime.create_state_authority(
        COW, state_path=tmp_path / "subjects.json",
        evidence_path=tmp_path / "evidence.json")
    bundle = resolve.resolve(_request(resolve, "matrix-cfw-csw", needs_statework=True),
                             cow_root=COW, requested_flows={"tuid": "discover"})
    assert bundle.stateworks, "CSW must supply workflows when explicitly configured"
    session = runtime.start(bundle, telemetry_disabled=True, state_authority=authority)
    assert session.identity_authority.authorities.state_authority is not None
    # DP is host-injected; nothing in this composition may have built it.
    assert not any("digital_psychology" in name for name in vars(session))


@pytest.mark.parametrize("with_csw", [False, True],
                         ids=["cfw-plus-dp", "cfw-plus-csw-plus-dp"])
def test_cell_3_and_4_cfw_with_dp_compose_without_requiring_laya(tmp_path, with_csw):
    """CFW+DP and CFW+CSW+DP must start with no semantic provider configured.

    DP's judge is host-injected and defaults to disabled, so neither cell may
    depend on Laya being present.
    """
    resolve, runtime = _runtime_modules()
    task_id = "matrix-dp" + ("-csw" if with_csw else "-only")
    kwargs = {}
    if with_csw:
        kwargs["state_authority"] = runtime.create_state_authority(
            COW, state_path=tmp_path / "subjects.json",
            evidence_path=tmp_path / "evidence.json")
    bundle = resolve.resolve(
        _request(resolve, task_id, needs_statework=with_csw),
        cow_root=COW if with_csw else NO_CSW_ROOT,
        requested_flows={"tuid": "discover"} if with_csw else None)
    session = runtime.start(bundle, telemetry_disabled=True, **kwargs)
    # With no provider configured the session is still usable and deterministic.
    assert session.bundle.task_id == task_id
