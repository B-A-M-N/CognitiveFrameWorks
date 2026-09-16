"""StateWork packet-freshness and subject-binding boundary."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional, Tuple


class SegmentStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ExecutionSegment:
    """Immutable host subcall boundary; one StateWork is active at a time."""

    segment_id: str
    index: int
    statework_id: Optional[str]
    subject_ref: Optional[str]
    input_packet: Optional[str] = None
    output_packet: Optional[str] = None
    exclusive_group: Optional[str] = None
    lifecycle_stages: Tuple[str, ...] = ()


def current_packet(store: Any, packet_type: str, *, task_id: str,
                   subject_ref: Optional[str],
                   observation_context: Optional[Mapping[str, Any]]) -> Optional[dict]:
    """Use only the authoritative current-state API.

    Schema-valid lookup remains available on PacketStore for diagnostics, but
    runtime planning must not silently downgrade to it.
    """
    if not subject_ref:
        return None
    return store.get_current(packet_type, task_id=task_id,
                             subject_ref=subject_ref,
                             observation_context=observation_context)
