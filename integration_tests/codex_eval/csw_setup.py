"""Disposable CSW/Gitter/Getter setup and evidence helpers."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Mapping

from contracts import validate_packet as _validate_packet
from contracts import validate_packet_schema as _validate_packet_schema
from control_plane import (
    EvidenceStore,
    SubjectStateStore,
    WorkflowAuthority,
    WorkflowSelectionStore,
)
from repository_truth import (
    active_worktrees as _active_worktrees,
    git as _statework_git,
    remote_fingerprint as _remote_fingerprint,
    repo_identity as _repo_identity,
    validate_repository_truth_packet,
    worktree_fingerprint as _worktree_fingerprint,
)
from runtime_contract.shared_csw_workflow_port import SharedCSWWorkflowPort
from runtime_contract.statework_provider import statework_provider
from runtime_contract.task import TaskRequest

from .common import CSW
from .fixture import _source_digest, _tracked_files


def _csw_selection_snapshot(authority: Any,
                            workflow_identities: Mapping[str, Mapping[str, Any]]
                            ) -> dict[str, Any] | None:
    """Capture bounded selection/ownership evidence directly from CSW."""
    if not workflow_identities:
        return None
    identity = workflow_identities[sorted(workflow_identities)[0]]
    raw = authority.behavioral_snapshot(
        workflow_scope=SharedCSWWorkflowPort._scope(identity),
        candidate_statework_ids=tuple(sorted(workflow_identities)))
    active_rows = [row for row in raw.get("active_workflows") or ()
                   if isinstance(row, Mapping)
                   and isinstance(row.get("identity"), Mapping)]
    active_ids = sorted(str(row["identity"]["statework_id"])
                        for row in active_rows
                        if isinstance(row["identity"].get("statework_id"), str))
    return {
        "selection_revision": int(raw.get("selection_revision", 0)),
        "active_workflow_ids": active_ids,
        "activation_owners": {
            str(row["identity"]["statework_id"]): str(row.get("activation_owner") or "")
            for row in active_rows
            if isinstance(row["identity"].get("statework_id"), str)},
        "dependency_required_by": {
            str(row["identity"]["statework_id"]): sorted(
                str(item) for item in row.get("dependency_required_by") or ())
            for row in active_rows
            if isinstance(row["identity"].get("statework_id"), str)
            and row.get("dependency_required_by")},
        "workflow_revisions": {
            str(row["identity"]["statework_id"]): int(row.get("revision", 0))
            for row in active_rows
            if isinstance(row["identity"].get("statework_id"), str)},
    }

def _complete_gitter_workflow(authority: Any,
                              workflow_identity: Mapping[str, Any],
                              workspace: Path, request: TaskRequest,
                              host_handle: Any) -> dict[str, Any]:
    """Attest current repository truth and publish Gitter's required handoff."""
    statework_id = str(workflow_identity.get("statework_id") or "")
    if statework_id != "gitter":
        raise RuntimeError("coding task selected an unsupported repository StateWork")
    contract_identity = authority.workflow_contract_identity(statework_id)
    contract_hash = str(contract_identity["contract_hash"])
    snapshot = authority.inspect_workflow(
        workflow_identity, contract_hash=contract_hash)
    already_complete = bool(snapshot.completed)
    if not already_complete and snapshot.current_state != "UNKNOWN":
        raise RuntimeError(
            f"Gitter cannot complete from state {snapshot.current_state!r}")

    status_text = _statework_git(workspace, "status", "--porcelain=v1",
                                 "--untracked-files=all")
    status_lines = [line for line in status_text.splitlines() if line]
    staged = sorted(line[3:] for line in status_lines
                    if len(line) >= 4 and line[0] != " " and line[:2] != "??")
    unstaged = sorted(line[3:] for line in status_lines
                      if len(line) >= 4 and line[1] != " " and line[:2] != "??")
    untracked = sorted(line[3:] for line in status_lines if line.startswith("?? "))
    branch = _statework_git(workspace, "branch", "--show-current").strip() or None
    head = _statework_git(workspace, "rev-parse", "HEAD").strip()
    tracking = _statework_git(
        workspace, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}",
        check=False).strip() or None
    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    task_id = request.task_id
    subject_ref = request.subject_ref or request.task_id
    freshness = {
        "repo_identity": _repo_identity(workspace),
        "observed_head": head,
        "working_tree_fingerprint": _worktree_fingerprint(workspace),
        "active_worktree_snapshot": _active_worktrees(workspace),
        "remote_state_fingerprint": _remote_fingerprint(workspace),
        "invalidation_conditions": ["HEAD changes", "working tree changes",
                                    "active worktrees change"],
    }
    packet = {
        "packet_id": "coding-truth-" + hashlib.sha256(
            f"{task_id}:{observed_at}".encode()).hexdigest()[:32],
        "packet_type": "repository_truth_packet",
        "schema_version": "1.1.0",
        "producer": "gitter",
        "task_id": task_id,
        "subject_ref": subject_ref,
        "observed_at": observed_at,
        "input_state": "UNKNOWN",
        "output_state": "OBSERVED",
        "evidence_refs": [],
        "unknowns": (["No upstream remote is configured; remote state is unknown."]
                     if freshness["remote_state_fingerprint"] is None else []),
        "blockers": [],
        "authority": "host-authored disposable Codex App Server trial snapshot",
        "invalidated_by": None,
        "recommended_next": None,
        "payload": {
            "repository": {
                "root": str(workspace.resolve()),
                "current_branch": branch,
                "head_sha": head,
                "tracking_branch": tracking,
                "target_base": None,
                "target_base_sha": None,
                "merge_base": None,
                "working_tree": {"clean": not bool(status_lines),
                                 "status_porcelain": status_lines},
                "staged_changes": staged,
                "unstaged_changes": unstaged,
                "untracked_files": untracked,
                "ahead": None,
                "behind": None,
                "local_only_commits": [],
                "remote_only_commits": [],
                "active_worktrees": [
                    line.partition(" ")[2]
                    for line in _statework_git(
                        workspace, "worktree", "list", "--porcelain").splitlines()
                    if line.startswith("worktree ")],
                "operation_in_progress": None,
                "remote_visibility": None,
            },
            "change": {
                "intended_task": str(request.goal_contract.objective),
                "branch_purpose": "Disposable multi-defect coding evaluation",
                "logical_change_units": [path for path in sorted(
                    {line[3:] for line in status_lines if len(line) >= 4})],
                "unrelated_changes": [],
                "unexpected_commits": [],
                "branch_owner": "host trial runner",
                "concurrent_workers": [],
                "rewrite_risk": "none",
                "integration_risk": "low",
            },
            "assessment": {
                "standard_assessment": (
                    "Disposable trial repository inspected after model edits; "
                    "the repository path and working-tree state are host measured."),
                "disposition": "safe",
            },
            "freshness": freshness,
        },
    }
    observation_ref = host_handle.observation(
        "gitter_repository_snapshot_observed", subject_ref,
        details={"head_sha": head,
                 "working_tree_fingerprint": freshness["working_tree_fingerprint"],
                 "observed_at": observed_at})
    packet["evidence_refs"] = [observation_ref]
    validation = validate_repository_truth_packet(packet, workspace)
    if validation.get("valid") is not True:
        raise RuntimeError(
            "host repository-truth validator rejected the trial snapshot: "
            + ", ".join(map(str, validation.get("invalid_fields", ())))
            + "; " + "; ".join(map(str, validation.get("schema_errors", ()))))
    encoded = json.dumps(packet, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    evidence_ref = "coding-repository-truth:" + hashlib.sha256(encoded).hexdigest()
    authority.evidence_store.create({
        "ref": evidence_ref,
        "kind": "repository_truth",
        "subject_ref": subject_ref,
        "issuer": "validator:repository-truth-v1",
        "validator": "repository-truth-v1",
        "schema": "repository-truth-packet.schema.json",
        "observed_at": observed_at,
        "content_digest": hashlib.sha256(encoded).hexdigest(),
        "packet": packet,
    })
    evidence_path = Path(authority.storage_components["evidence_store"])
    authority.evidence_store.save(evidence_path)
    if not already_complete:
        decision = authority.apply_step(
            workflow_identity, contract_hash=contract_hash,
            step_id="gitter.unknown.observe.observed",
            evidence_refs=(evidence_ref,), expected_revision=snapshot.revision)
        if not decision.allowed or not decision.completed:
            raise RuntimeError("CSW rejected the validated Gitter repository observation")
        revision = decision.revision
    else:
        revision = snapshot.revision
    cfw_truth_ref = host_handle.repository_truth(
        packet, {"repository_path": str(workspace.resolve()),
                 "repository_freshness": freshness}, subject_ref=subject_ref)
    handoff_id = host_handle.publish_handoff(
        packet, observation_context={
            "repository_path": str(workspace.resolve()),
            "repository_freshness": freshness})
    return {"statework_id": statework_id, "completed": True,
            "already_complete": already_complete, "revision": revision,
            "repository_freshness": freshness,
            "evidence_ref": evidence_ref,
            "cfw_repository_truth_evidence_ref": cfw_truth_ref,
            "cfw_handoff_packet_id": handoff_id,
            "repository_truth_valid": True,
            "observed_paths": len(status_lines)}

def _share_csw_evidence(authority: Any, *, evidence_ref: str, evidence_kind: str,
                        subject_ref: str, issuer: str, validator: str | None,
                        schema: str | None, content: Mapping[str, Any],
                        observed_at: str | None = None) -> str:
    """Copy a host-issued CFW evidence reference into the shared CSW store."""
    timestamp = observed_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    encoded = json.dumps(dict(content), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, default=str).encode("utf-8")
    authority.evidence_store.create({
        "ref": evidence_ref,
        "kind": evidence_kind,
        "subject_ref": subject_ref,
        "issuer": issuer,
        "validator": validator,
        "schema": schema,
        "observed_at": timestamp,
        "content_digest": hashlib.sha256(encoded).hexdigest(),
        "content": dict(content),
    })
    evidence_path = Path(authority.storage_components["evidence_store"])
    authority.evidence_store.save(evidence_path)
    return evidence_ref

def _apply_getter_step(authority: Any, identity: Mapping[str, Any], *,
                       contract_hash: str, step_id: str,
                       evidence_refs: tuple[str, ...]) -> dict[str, Any]:
    before = authority.inspect_workflow(identity, contract_hash=contract_hash)
    decision = authority.apply_step(
        identity, contract_hash=contract_hash, step_id=step_id,
        evidence_refs=evidence_refs, expected_revision=before.revision)
    if not decision.allowed:
        raise RuntimeError(
            f"CSW rejected Getter step {step_id!r}: {decision.reason}")
    after = authority.inspect_workflow(identity, contract_hash=contract_hash)
    return {"step_id": step_id, "from_state": before.current_state,
            "to_state": after.current_state, "revision": after.revision,
            "completed": bool(after.completed)}

def _prepare_getter_workflow(authority: Any, workflow_identity: Mapping[str, Any],
                             workspace: Path, request: TaskRequest,
                             host_handle: Any, *,
                             gitter_result: Mapping[str, Any]) -> dict[str, Any]:
    """Use host task policy and a disposable checkout to open Getter's change gate."""
    statework_id = str(workflow_identity.get("statework_id") or "")
    if statework_id != "getter":
        raise RuntimeError("multi-path coding trial requires the Getter workflow")
    contract = authority.workflow_contract_identity(statework_id)
    contract_hash = str(contract["contract_hash"])
    snapshot = authority.inspect_workflow(
        workflow_identity, contract_hash=contract_hash)
    if snapshot.current_state != "OBSERVED" or snapshot.completed:
        raise RuntimeError(
            "Getter must start from OBSERVED after the host records repository truth")

    subject_ref = request.subject_ref or request.task_id
    seed_head = _statework_git(workspace, "rev-parse", "HEAD").strip()
    initial_source_sha = _source_digest(workspace)
    source_root = str((workspace / "src").resolve())
    intent = {
        "objective": str(request.goal_contract.objective),
        "authorized_write_roots": [source_root],
        "non_goals": list(request.goal_contract.non_goals),
        "repository_truth_ref": str(gitter_result.get("evidence_ref") or ""),
    }
    risk = {
        "risk_class": "low",
        "workspace_disposable": True,
        "remote_configured": bool(_remote_fingerprint(workspace)),
        "writable_roots": [source_root],
        "mitigations": [
            "host-enforced source-only write boundary",
            "all other fixture files are protected",
            "seed commit is retained as a restore point",
            "trial workspace is deleted after result capture",
        ],
    }
    intent_ref = host_handle.observation("intent", subject_ref, details=intent)
    risk_ref = host_handle.observation("risk", subject_ref, details=risk)
    _share_csw_evidence(
        authority, evidence_ref=intent_ref, evidence_kind="intent",
        subject_ref=subject_ref, issuer="host:observation", validator=None,
        schema=None, content=intent)
    _share_csw_evidence(
        authority, evidence_ref=risk_ref, evidence_kind="risk",
        subject_ref=subject_ref, issuer="host:observation", validator=None,
        schema=None, content=risk)
    plan_step = _apply_getter_step(
        authority, workflow_identity, contract_hash=contract_hash,
        step_id="getter.observed.plan.planned",
        evidence_refs=(intent_ref, risk_ref))

    plan_id = "getter-rollback-" + hashlib.sha256(
        f"{request.task_id}:{seed_head}".encode("utf-8")).hexdigest()[:24]
    issued_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    authorization = {
        "authorization_id": f"{request.task_id}:source-write",
        "principal": "generic-codex-evaluation-host",
        "scope": {"workspace": str(workspace.resolve()),
                  "write_roots": [source_root],
                  "operation": "repair seeded source defects"},
        "decision": "allow",
        "issued_at": issued_at,
    }
    authorization_ref = host_handle.authorization(
        authorization, subject_ref=subject_ref)
    _share_csw_evidence(
        authority, evidence_ref=authorization_ref, evidence_kind="authority",
        subject_ref=subject_ref, issuer="host:authorization",
        validator="host-authorization-v1", schema="authority-record-v1",
        content=authorization, observed_at=issued_at)

    rollback_plan = {
        "plan_id": plan_id,
        "intended_state": {"head_sha": seed_head,
                           "source_sha256": initial_source_sha,
                           "working_tree": "seeded and clean"},
        "steps": [
            f"restore tracked source files from seed commit {seed_head}",
            "remove any untracked files under src/ in this disposable workspace",
            "verify the seed commit and all protected fixture files",
            "discard the isolated workspace after the trial",
        ],
    }
    rollback_plan_ref = host_handle.rollback_plan(
        rollback_plan, subject_ref=subject_ref)
    _share_csw_evidence(
        authority, evidence_ref=rollback_plan_ref,
        evidence_kind="rollback_plan", subject_ref=subject_ref,
        issuer="host:rollback-plan", validator="rollback-plan-v1",
        schema="rollback-plan-v1", content=rollback_plan)
    rollback = {
        "plan_id": plan_id,
        "result": "available",
        "restored_state": {"head_sha": seed_head,
                            "source_sha256": initial_source_sha,
                            "restore_point_verified": True,
                            "workspace_disposable": True},
    }
    rollback_ref = host_handle.rollback(rollback, subject_ref=subject_ref)
    _share_csw_evidence(
        authority, evidence_ref=rollback_ref, evidence_kind="rollback",
        subject_ref=subject_ref, issuer="host:rollback", validator="rollback-v1",
        schema="rollback-record-v1", content=rollback)
    changing_step = _apply_getter_step(
        authority, workflow_identity, contract_hash=contract_hash,
        step_id="getter.planned.mutate.changing",
        evidence_refs=(authorization_ref, rollback_ref))
    return {
        "statework_id": "getter", "started": True,
        "seed_head": seed_head, "initial_source_sha256": initial_source_sha,
        "evidence_refs": {"intent": intent_ref, "risk": risk_ref,
                          "authority": authorization_ref,
                          "rollback": rollback_ref,
                          "rollback_plan": rollback_plan_ref},
        "steps": [plan_step, changing_step],
    }

def _complete_getter_workflow(authority: Any,
                              workflow_identity: Mapping[str, Any],
                              workspace: Path, request: TaskRequest,
                              host_handle: Any, *,
                              prepared: Mapping[str, Any],
                              contract: Mapping[str, Any],
                              validator_results: list[dict[str, Any]],
                              gitter_evidence_ref: str) -> dict[str, Any]:
    """Advance Getter only after scope, integrity, and host validators pass."""
    statework_id = str(workflow_identity.get("statework_id") or "")
    if statework_id != "getter":
        raise RuntimeError("repository evidence can only complete the Getter workflow")
    contract_identity = authority.workflow_contract_identity(statework_id)
    contract_hash = str(contract_identity["contract_hash"])
    before = authority.inspect_workflow(
        workflow_identity, contract_hash=contract_hash)
    if before.completed:
        return {"statework_id": "getter", "completed": True,
                "already_complete": True, "state": before.current_state,
                "revision": before.revision}
    if before.current_state != "CHANGING":
        return {"statework_id": "getter", "completed": False,
                "state": before.current_state,
                "reason": "Getter is not in CHANGING"}

    required_validators = {str(item["validator"])
                           for item in contract.get("criteria", ())}
    latest: dict[str, Mapping[str, Any]] = {}
    for item in validator_results:
        validator_id = str(item.get("validator") or "")
        if validator_id in required_validators:
            latest[validator_id] = item
    missing = sorted(required_validators - set(latest))
    failed = sorted(validator_id for validator_id, item in latest.items()
                    if item.get("status") != "passed"
                    or item.get("tests_failed", 0) != 0)
    stale = sorted(validator_id for validator_id, item in latest.items()
                   if not isinstance(item.get("source_sha256"), str)
                   or not item.get("source_sha256"))
    if missing or failed or stale:
        return {"statework_id": "getter", "completed": False,
                "state": before.current_state,
                "reason": "required host validators are incomplete, failing, or unbound",
                "missing_validators": missing, "failed_validators": failed,
                "validators_without_source_digest": stale}

    seed_head = str(prepared.get("seed_head") or "")
    initial_source_sha = str(prepared.get("initial_source_sha256") or "")

    def observe_repository() -> dict[str, Any]:
        status_text = _statework_git(
            workspace, "status", "--porcelain=v1", "--untracked-files=all")
        status_lines = [line for line in status_text.splitlines() if line]
        changed_paths = _tracked_files(workspace)
        out_of_scope = sorted(path for path in changed_paths
                              if path != "src" and not path.startswith("src/"))
        head = _statework_git(workspace, "rev-parse", "HEAD").strip()
        diff_check = subprocess.run(
            ["git", "diff", "--check", seed_head, "--"], cwd=workspace,
            capture_output=True, text=True, check=False)
        return {
            "head": head,
            "status_lines": status_lines,
            "changed_paths": changed_paths,
            "out_of_scope_paths": out_of_scope,
            "diff_check_passed": diff_check.returncode == 0,
            "diff_check_output": (diff_check.stdout + diff_check.stderr)[-512:],
            "source_sha256": _source_digest(workspace),
            "worktree_fingerprint": _worktree_fingerprint(workspace),
        }

    verified_facts = observe_repository()
    checks_passed = bool(
        verified_facts["head"] == seed_head
        and verified_facts["source_sha256"] != initial_source_sha
        and all(item.get("source_sha256") == verified_facts["source_sha256"]
                for item in latest.values())
        and not verified_facts["out_of_scope_paths"]
        and verified_facts["diff_check_passed"])
    if not checks_passed:
        return {"statework_id": "getter", "completed": False,
                "state": before.current_state,
                "reason": "repository evidence did not meet the bounded mutation contract",
                "head_matches_seed": verified_facts["head"] == seed_head,
                "source_changed": verified_facts["source_sha256"] != initial_source_sha,
                "validator_source_digests_match": all(
                    item.get("source_sha256") == verified_facts["source_sha256"]
                    for item in latest.values()),
                "out_of_scope_paths": verified_facts["out_of_scope_paths"],
                "diff_check_passed": verified_facts["diff_check_passed"]}

    validator_summary = {
        validator_id: {"status": str(latest[validator_id].get("status") or ""),
                       "tests_passed": int(latest[validator_id].get("tests_passed", 0)),
                       "tests_failed": int(latest[validator_id].get("tests_failed", 0))}
        for validator_id in sorted(required_validators)
    }
    schema_path, evidence_schema = _validate_packet_schema(
        CSW / "getter", {"schema": "contracts/schemas/repository-evidence-packet.schema.json"})
    del schema_path
    handoff_schema = json.loads(
        (CSW / "schemas" / "handoff-packet.schema.json").read_text(encoding="utf-8"))

    def record_repository_evidence(input_state: str, output_state: str,
                                   prior_refs: tuple[str, ...],
                                   facts: Mapping[str, Any]) -> str:
        observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        evidence_observation = host_handle.observation(
            "getter_repository_evidence_checked", request.subject_ref,
            details={"observed_head": facts["head"],
                     "source_sha256": facts["source_sha256"],
                     "worktree_fingerprint": facts["worktree_fingerprint"],
                     "validators": validator_summary,
                     "out_of_scope_paths": facts["out_of_scope_paths"],
                     "diff_check_passed": facts["diff_check_passed"]})
        packet = {
            "packet_id": "coding-getter-" + hashlib.sha256(
                f"{request.task_id}:{observed_at}:{output_state}".encode("utf-8")
            ).hexdigest()[:32],
            "packet_type": "repository_evidence_packet",
            "schema_version": "1.1.0",
            "producer": "getter",
            "task_id": request.task_id,
            "subject_ref": request.subject_ref or request.task_id,
            "observed_at": observed_at,
            "input_state": input_state,
            "output_state": output_state,
            "evidence_refs": [evidence_observation, *prior_refs],
            "unknowns": [],
            "blockers": [],
            "authority": "generic Codex App Server host validator and repository observer",
            "invalidated_by": None,
            "recommended_next": ("stabilize" if output_state == "VERIFIED" else None),
            "payload": {
                "operation": "codex_source_edit",
                "result": "success",
                "observed_head": str(facts["head"]),
                "evidence": [
                    evidence_observation,
                    *prior_refs,
                    f"source-sha256:{facts['source_sha256']}",
                    f"worktree-fingerprint:{facts['worktree_fingerprint']}",
                    "git-diff-check:passed",
                    *[f"validator:{name}:passed:{summary['tests_passed']}"
                      for name, summary in validator_summary.items()],
                ],
                "notes": "Host validators passed on the unchanged, source-only trial workspace.",
            },
        }
        if output_state == "STABLE":
            final_handoff_packet.clear()
            final_handoff_packet.update(packet)
        _validate_packet(packet, evidence_schema,
                         reference_schemas=(handoff_schema,))
        validation = {
            "valid": True,
            "validator": "repository-evidence-v1",
            "checks": {"packet_schema": True,
                       "required_validators_passed": True,
                       "scope_within_src": True,
                       "git_diff_check": True,
                       "head_matches_seed": True,
                       "validator_source_digests_match": True},
            "source_sha256": facts["source_sha256"],
            "worktree_fingerprint": facts["worktree_fingerprint"],
            "validator_results": validator_summary,
        }
        evidence_ref = host_handle.repository_evidence(
            packet, validation, subject_ref=request.subject_ref)
        _share_csw_evidence(
            authority, evidence_ref=evidence_ref,
            evidence_kind="repository_evidence",
            subject_ref=request.subject_ref or request.task_id,
            issuer="validator:repository-evidence-v1",
            validator="repository-evidence-v1",
            schema="repository-evidence-packet.schema.json",
            content={"packet": packet, "validation": validation},
            observed_at=observed_at)
        return evidence_ref

    final_handoff_packet: dict[str, Any] = {}
    verified_ref = record_repository_evidence(
        "CHANGING", "VERIFIED",
        (gitter_evidence_ref,), verified_facts)
    verified_step = _apply_getter_step(
        authority, workflow_identity, contract_hash=contract_hash,
        step_id="getter.changing.validate.verified",
        evidence_refs=(verified_ref,))

    stable_facts = observe_repository()
    stable_matches = all(stable_facts[key] == verified_facts[key] for key in (
        "head", "status_lines", "changed_paths", "out_of_scope_paths",
        "diff_check_passed", "source_sha256", "worktree_fingerprint"))
    if not stable_matches:
        failed_ref = host_handle.observation(
            "failed_verification", request.subject_ref,
            details={"reason": "repository changed after verification",
                     "verified_fingerprint": verified_facts["worktree_fingerprint"],
                     "current_fingerprint": stable_facts["worktree_fingerprint"]})
        _share_csw_evidence(
            authority, evidence_ref=failed_ref, evidence_kind="failed_verification",
            subject_ref=request.subject_ref or request.task_id,
            issuer="host:observation", validator=None, schema=None,
            content={"verified_facts": verified_facts,
                     "current_facts": stable_facts})
        recovery_step = _apply_getter_step(
            authority, workflow_identity, contract_hash=contract_hash,
            step_id="getter.verified.recover.recovering",
            evidence_refs=(failed_ref,))
        return {"statework_id": "getter", "completed": False,
                "state": recovery_step["to_state"],
                "reason": "repository changed after verification",
                "steps": [verified_step, recovery_step]}

    stable_ref = record_repository_evidence(
        "VERIFIED", "STABLE", (verified_ref,), stable_facts)
    stable_step = _apply_getter_step(
        authority, workflow_identity, contract_hash=contract_hash,
        step_id="getter.verified.stabilize.stable",
        evidence_refs=(stable_ref,))
    if not final_handoff_packet:
        raise RuntimeError("Getter completed without a final repository evidence packet")
    handoff_id = host_handle.publish_handoff(final_handoff_packet)
    final = authority.inspect_workflow(
        workflow_identity, contract_hash=contract_hash)
    return {"statework_id": "getter", "completed": bool(final.completed),
            "state": final.current_state, "revision": final.revision,
            "cfw_handoff_packet_id": handoff_id,
            "repository_evidence_refs": [verified_ref, stable_ref],
            "steps": [verified_step, stable_step],
            "validator_results": validator_summary,
            "observed_paths": len(verified_facts["status_lines"])}

def _make_csw(request: TaskRequest, root: Path) -> tuple[Any, Any, Any]:
    state_root = root / "csw-state"
    state_root.mkdir(mode=0o700)
    state_path = state_root / "subjects.sqlite3"
    selection_path = state_root / "selections.sqlite3"
    evidence_path = state_root / "evidence.json"
    state_store = SubjectStateStore(state_path)
    evidence_store = EvidenceStore()
    evidence_store.save(evidence_path)
    authority = WorkflowAuthority(
        root=CSW, state_store=state_store, evidence_store=evidence_store,
        selection_store=WorkflowSelectionStore(selection_path),
        storage_identity={"state_store": str(state_path.resolve()),
                          "selection_store": str(selection_path.resolve()),
                          "evidence_store": str(evidence_path.resolve())})
    provider = statework_provider(CSW)
    state_authority = provider.create_state_authority(
        state_path=state_path, evidence_path=evidence_path,
        namespace_id=request.namespace_id, application_id=request.application_id,
        application_instance_id=request.application_instance_id,
        workflow_id=request.task_id, workflow_authority=authority)
    workflow_port = SharedCSWWorkflowPort(provider, state_authority, authority)
    return authority, state_authority, workflow_port
