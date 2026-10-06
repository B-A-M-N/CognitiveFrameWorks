"""Bind trusted state and workflow authorities for a started CFW session."""
from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Dict, Mapping

from runtime_session.resolver_bridge import resolve_runtime
from runtime_session.state import AuthorityBindings


@dataclass(frozen=True)
class BoundAuthorities:
    state_authority: Any
    workflow_port: Any


def bind_start_authorities(
        session: Any, bundle: Any, *, runtime_mode: str,
        state_authority: Any, membership_authority: Any,
        policy_revocation_authority: Any, operator_read_model: Any,
        ) -> BoundAuthorities:
    """Validate and bind workflow authority without granting CFW local truth."""
    if runtime_mode == "combined_enforcing":
        if state_authority is None:
            raise ValueError(
                "combined_enforcing requires an injected shared StateAuthority")
        if not bundle.stateworks:
            raise ValueError(
                "combined_enforcing requires at least one StateWork-backed task")
        if callable(getattr(state_authority, "bind_scope", None)):
            memberships = list(session.identity_authority.authorities.shared_workflow_memberships)
            if (session.identity_authority.authorities.shared_workflow_membership
                    and not any(dict(item) == dict(session.identity_authority.authorities.shared_workflow_membership)
                                for item in memberships)):
                memberships.append(MappingProxyType(
                    dict(session.identity_authority.authorities.shared_workflow_membership)))
            expected_subjects = dict(bundle.pinned.get("statework_subject_refs") or {})
            authorized_memberships: Dict[str, Mapping[str, Any]] = {}
            active_stateworks = {str(item.get("id")) for item in bundle.stateworks}
            for membership in memberships:
                statework_id = str(membership.get("statework_id") or "")
                if not statework_id or statework_id not in active_stateworks:
                    raise ValueError(
                        "shared workflow membership names an inactive StateWork")
                expected_subject = expected_subjects.get(
                    statework_id, bundle.policy.subject_ref)
                for field, expected in (
                        ("namespace_id", bundle.pinned["namespace_id"]),
                        ("application_id", bundle.pinned["application_id"]),
                        ("application_instance_id",
                         bundle.pinned["application_instance_id"]),
                        ("subject_ref", expected_subject)):
                    if membership.get(field) != expected:
                        raise ValueError(
                            f"shared workflow membership {field} differs from its "
                            "StateWork binding")
                if statework_id in authorized_memberships:
                    raise ValueError(
                        "a StateWork cannot have multiple workflow memberships")
                if (membership_authority is None
                        or not membership_authority.validate(
                            membership, session=session, operation="transition")):
                    raise PermissionError(
                        "shared workflow membership is not host-authorized")
                authorized_memberships[statework_id] = membership
            if len(authorized_memberships) == 1:
                workflow_id = str(next(iter(authorized_memberships.values()))[
                    "workflow_id"])
            else:
                workflow_id = (
                    f"private:{bundle.pinned['namespace_id']}:"
                    f"{bundle.pinned['application_instance_id']}:"
                    f"{session.identity_authority.identity.agent_instance_id}")
            state_authority = state_authority.bind_scope(
                namespace_id=bundle.pinned["namespace_id"],
                application_id=bundle.pinned["application_id"],
                application_instance_id=bundle.pinned["application_instance_id"],
                workflow_id=workflow_id,
                statework_memberships=authorized_memberships)
            if authorized_memberships:
                shared_branch_path = (
                    resolve_runtime.runtime_state_dir() / "shared-branches.sqlite3")
                from runtime_contract.convergence import SharedBranchLedger
                session.attempts.bind_shared_branch_ledger(
                    SharedBranchLedger(shared_branch_path))

    if (runtime_mode == "standalone_enforcing" and bundle.stateworks
            and state_authority is None):
        raise ValueError(
            "standalone_enforcing with StateWorks requires an explicitly injected "
            "StateAuthority")
    if (runtime_mode != "combined_enforcing"
            and callable(getattr(state_authority, "bind_scope", None))):
        workflow_id = getattr(state_authority, "workflow_id", None)
        if not workflow_id:
            workflow_id = (
                f"private:{bundle.pinned.get('namespace_id', 'default')}:"
                f"{bundle.pinned.get('application_instance_id', 'unknown')}:"
                f"{session.identity_authority.identity.agent_instance_id}")
        state_authority = state_authority.bind_scope(
            namespace_id=bundle.pinned.get("namespace_id", "default"),
            application_id=bundle.pinned.get(
                "application_id", "cognitiveframeworks-runtime"),
            application_instance_id=bundle.pinned.get(
                "application_instance_id", "cognitiveframeworks-runtime"),
            workflow_id=workflow_id,
            statework_memberships={
                str(item.get("statework_id")): item
                for item in session.identity_authority.authorities.shared_workflow_memberships})
    if runtime_mode == "standalone_advisory" and state_authority is not None:
        raise ValueError(
            "standalone_advisory cannot combine with shared StateWork authority")

    workflow_port = getattr(bundle, "workflow_port", None)
    bind_authority = getattr(workflow_port, "bind_state_authority", None)
    workflow_port = (bind_authority(state_authority)
                     if callable(bind_authority) and state_authority is not None
                     else workflow_port)
    session.identity_authority = replace(
        session.identity_authority,
        authorities=replace(
            session.identity_authority.authorities,
            state_authority=state_authority,
            workflow_port=workflow_port,
            policy_revocation_authority=policy_revocation_authority,
            membership_authority=membership_authority,
            operator_read_model=operator_read_model,
            runtime_mode=runtime_mode,
        ),
    )
    if runtime_mode == "combined_enforcing" and not session._authoritative_state_ready():
        raise ValueError("shared StateWork authority could not resolve initial state")
    return BoundAuthorities(state_authority, workflow_port)
