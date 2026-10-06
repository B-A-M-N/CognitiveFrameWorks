"""Prepare stable session identity and host context during CFW startup."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from runtime_session.journal import _journal_identity_prefix
from runtime_session.resolver_bridge import resolve_runtime


@dataclass(frozen=True)
class StartIdentity:
    session_nonce: str
    session_id: str
    safe_task: str
    journal_root: Path
    host_context: Any


def prepare_start_identity(
        bundle: Any, *, supplied_context: Any,
        session_nonce: str | None, session_id: str | None,
        resume: bool) -> StartIdentity:
    """Bind caller grants to this immutable task, policy, and session."""
    if (session_nonce is not None and supplied_context.session_nonce
            and session_nonce != supplied_context.session_nonce):
        raise ValueError(
            "session nonce supplied by host context and resume request differ")
    session_nonce = (supplied_context.session_nonce or session_nonce
                     or f"nonce-{uuid.uuid4()}")
    safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "_", bundle.task_id)[:128] or "task"
    journal_root = resolve_runtime.runtime_state_dir() / "sessions"
    if resume and session_id is None:
        # Restore analytics identity from the authority journal. Never infer it
        # from the security nonce or task ID.
        journal_db = journal_root / (
            f"session-{_journal_identity_prefix(bundle)}-{safe_task}-"
            f"{session_nonce}.sqlite3")
        if journal_db.exists():
            with sqlite3.connect(journal_db) as database:
                row = database.execute(
                    "SELECT event_json FROM authority_events ORDER BY seq LIMIT 1"
                ).fetchone()
            if row:
                session_id = json.loads(row[0]).get("session_id")
    session_id = session_id or f"analytics-{uuid.uuid4()}"
    if supplied_context.confirmation_grants and (
            supplied_context.task_id != bundle.task_id
            or supplied_context.policy_hash != bundle.pinned["policy_hash"]
            or supplied_context.session_nonce != session_nonce):
        raise ValueError(
            "confirmation grants must be bound to this task policy and session")
    bound_context = replace(
        supplied_context, task_id=bundle.task_id,
        policy_hash=bundle.pinned["policy_hash"],
        session_nonce=session_nonce)
    return StartIdentity(session_nonce, session_id, safe_task,
                         journal_root, bound_context)
