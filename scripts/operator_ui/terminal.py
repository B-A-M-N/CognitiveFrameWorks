"""Keyboard-friendly terminal cockpit using the shared operator service."""
from __future__ import annotations

from typing import Any

ICONS = {"running": "●", "waiting": "…", "blocked": "■", "recovering": "↻",
         "completed": "✓", "failed": "✕", "disconnected": "○", "unknown": "?"}

def render(snapshot: dict[str, Any]) -> str:
    tasks = snapshot.get("tasks", [])
    attention = sum(1 for task in tasks if task.get("status") in {"blocked", "failed", "disconnected"})
    lines = ["Cognitive Cockpit", f"Connected · {len(tasks)} active · {attention} attention",
             "Agent activity"]
    for task in tasks:
        agent = task.get("agent", {})
        lines.append(f"  {ICONS.get(task.get('status'), '?')} {agent.get('agent_instance_id', task.get('task_id'))} · {task.get('status')} · {agent.get('task_id')}")
    lines.extend(["", "Shared learning"])
    for profile in snapshot.get("profiles", []):
        lines.append(f"  {profile.get('status', 'unknown')} · {profile.get('profile_id', 'profile')} · adoption explicit")
    lines.extend(["", "Health", f"  Events through {snapshot.get('health', {}).get('operator_event_high_watermark', 0)} · read-only"])
    return "\n".join(lines)

def run(service: Any) -> int:
    print(render(service.snapshot()))
    input("q/Enter to exit: ")
    service.close()
    return 0
