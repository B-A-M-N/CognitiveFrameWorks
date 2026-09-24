"""Host-owned action binding and authority contracts."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


_GRANT_SECRET = secrets.token_bytes(32)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _normalized_arguments(arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """Canonicalize action arguments before grants bind to their digest."""
    normalized = dict(arguments)
    for key in ("target", "path", "resource"):
        value = normalized.get(key)
        if isinstance(value, str) and value:
            normalized[key] = os.path.normpath(value)
    return normalized


def action_digest(binding_id: str, arguments: Mapping[str, Any],
                 subject_ref: Optional[str]) -> str:
    payload = {
        "binding_id": str(binding_id),
        "arguments": _normalized_arguments(arguments),
        "subject_ref": subject_ref,
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProtectedResourcePolicy:
    """Host-owned filesystem boundary for mutation-capable tools.

    All comparisons use resolved paths.  This catches absolute paths,
    traversal and symlink escapes without special-casing one filename.
    """

    protected_roots: tuple[str, ...] = ()
    authorized_write_roots: tuple[str, ...] = ()

    @classmethod
    def default(cls, runtime_root: Optional[Path] = None) -> "ProtectedResourcePolicy":
        home = Path.home()
        state_home = Path(os.environ.get("XDG_STATE_HOME", home / ".local" / "state"))
        xdg_runtime = Path(os.environ["XDG_RUNTIME_DIR"]) if os.environ.get("XDG_RUNTIME_DIR") else None
        runtime = runtime_root or (xdg_runtime / "cognitiveframeworks" if xdg_runtime else
                                   Path(f"/tmp/cognitiveframeworks-{os.getuid()}"))
        roots = {
            state_home / "digitalpsychology",
            runtime,
            home / ".codex" / "skills" / "cognitiveframeworks_runtime",
            home / ".agents" / "skills" / "cognitiveframeworks_runtime",
        }
        configured_registry = os.environ.get("COGNITIVE_RUNTIME_REGISTRY")
        if configured_registry:
            roots.add(Path(configured_registry).expanduser() / "cognitiveframeworks_runtime")
        configured_dp_root = os.environ.get("DIGITALPSYCHOLOGY_STATE_ROOT")
        if configured_dp_root:
            roots.add(Path(configured_dp_root))
        # Source/runtime contract roots are protected too, including sibling
        # StateWork installations when this is running from a checkout.
        module_root = Path(__file__).resolve().parents[2]
        bundled_cow = module_root / "statework"
        cow_root = bundled_cow if bundled_cow.exists() else module_root.parent / "CognitiveStateWork"
        roots.update({module_root / "contracts", module_root / "scripts" / "runtime_contract",
                      cow_root, cow_root / "schemas"})
        return cls(tuple(sorted(str(path) for path in roots)),
                   (str(Path.cwd()), "/tmp"))

    @staticmethod
    def _resolved(path: Path) -> Path:
        return path.expanduser().resolve(strict=False)

    @staticmethod
    def _inside(path: Path, roots: tuple[str, ...]) -> bool:
        for raw_root in roots:
            root = ProtectedResourcePolicy._resolved(Path(raw_root))
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def target_path(self, arguments: Mapping[str, Any]) -> Optional[Path]:
        raw = arguments.get("target") or arguments.get("path") or arguments.get("resource")
        if not isinstance(raw, str) or not raw.strip():
            return None
        path = Path(raw).expanduser()
        if not path.is_absolute():
            # Relative paths are resolved against the first host-authorized
            # root, never against a model-supplied working directory.
            base = Path(self.authorized_write_roots[0]) if self.authorized_write_roots else Path.cwd()
            path = base / path
        return self._resolved(path)

    def classify_write(self, arguments: Mapping[str, Any]) -> str:
        target = self.target_path(arguments)
        if target is None:
            return "missing_target"
        if self._inside(target, self.protected_roots):
            return "protected"
        if not self._inside(target, self.authorized_write_roots):
            return "outside_scope"
        return "authorized"


@dataclass(frozen=True)
class ActionDescriptor:
    """Trusted descriptor registered by the host for a tool binding."""

    tool_type: str
    mutability: str = "read"
    target_subject: Optional[str] = None
    trust_boundary: str = "local"
    blast_radius: str = "task"
    reversibility: str = "reversible"
    secret_access: bool = False
    network_effect: bool = False
    destructive: bool = False
    outside_scope: bool = False
    untrusted: bool = False
    required_authority: Optional[str] = None
    allowed_authorities: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()

    def for_request(self, request: "AgentActionRequest") -> "ActionDescriptor":
        """Return a target-sensitive classification derived by the host."""
        args = request.arguments
        target = str(args.get("target") or args.get("path") or args.get("resource") or "")
        if self.target_subject and request.subject_ref not in {None, self.target_subject}:
            return ActionDescriptor(**{**self.__dict__, "outside_scope": True})
        lowered_target = target.lower()
        sensitive_name = any(token in lowered_target for token in (
            "secret", "credential", "password", "token", "api_key", ".ssh"))
        if (target.startswith(("/etc/", "/var/", "/root/"))
                or lowered_target.startswith(("prod:", "production:"))
                or sensitive_name):
            return ActionDescriptor(
                **{**self.__dict__, "secret_access": True,
                   "required_authority": self.required_authority or "operator"})
        if self.network_effect or str(args.get("environment", "")).lower() in {"prod", "production"}:
            return ActionDescriptor(
                **{**self.__dict__, "network_effect": True,
                   "required_authority": self.required_authority or "operator"})
        return self


def action_contract_hash(registry: Optional[Mapping[str, ActionDescriptor]] = None) -> str:
    """Hash the host action classification contract, not model input."""
    source = registry or {}
    payload = {
        key: {
            field: getattr(descriptor, field)
            for field in (
                "tool_type", "mutability", "target_subject", "trust_boundary",
                "blast_radius", "reversibility", "secret_access", "network_effect",
                "destructive", "outside_scope", "untrusted", "required_authority",
                "allowed_authorities", "required_capabilities",
            )
        }
        for key, descriptor in sorted(source.items())
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def default_action_registry() -> Dict[str, ActionDescriptor]:
    """Canonical host bindings shared by policy composition and execution."""
    return {
        "read": ActionDescriptor("read"),
        "inspect": ActionDescriptor("inspect"),
        "test": ActionDescriptor("test"),
        "write": ActionDescriptor("write", mutability="write"),
        "mutate": ActionDescriptor("mutate", mutability="mutate",
                                     required_authority="operator"),
        "delete": ActionDescriptor("delete", mutability="delete", destructive=True,
                                     required_authority="operator",
                                     reversibility="irreversible"),
        "deploy": ActionDescriptor("deploy", mutability="deploy", outside_scope=True,
                                     required_authority="operator", network_effect=True),
        "shell": ActionDescriptor("shell", mutability="execute", untrusted=True,
                                    required_authority="operator"),
    }


def effective_action_registry(
        extensions: Optional[Mapping[str, ActionDescriptor]] = None
    ) -> Dict[str, ActionDescriptor]:
    """Build the host registry before policy composition.

    Built-in bindings are part of the runtime security contract and cannot be
    replaced by a downstream caller.  Integrations may add new bindings, but
    changing the meaning of ``shell``/``delete``/``deploy`` after resolution
    is rejected rather than silently producing a different enforcement plane.
    """
    registry = default_action_registry()
    for binding_id, descriptor in (extensions or {}).items():
        if not isinstance(binding_id, str) or not binding_id:
            raise ValueError("action binding ids must be non-empty strings")
        if not isinstance(descriptor, ActionDescriptor):
            raise TypeError(f"action binding {binding_id!r} must use ActionDescriptor")
        if binding_id in registry:
            if descriptor != registry[binding_id]:
                raise ValueError(
                    f"built-in action binding {binding_id!r} cannot be redefined")
            continue
        if descriptor.tool_type != binding_id:
            raise ValueError(
                f"extension action {binding_id!r} must declare the same tool_type")
        registry[binding_id] = descriptor
    return registry


@dataclass(frozen=True)
class AgentActionRequest:
    """Untrusted model request; it contains no authority or confirmation."""

    binding_id: str
    arguments: Mapping[str, Any]
    subject_ref: Optional[str] = None

    @classmethod
    def from_mapping(cls, action: Mapping[str, Any]) -> "AgentActionRequest":
        binding_id = action.get("tool_binding_id") or action.get("tool") or action.get("tool_type")
        if not isinstance(binding_id, str) or not binding_id:
            raise ValueError("action request requires a registered tool binding")
        raw_args = action.get("arguments", {})
        if not isinstance(raw_args, Mapping):
            raise ValueError("action arguments must be a mapping")
        subject = action.get("subject_ref", action.get("subject"))
        return cls(binding_id=binding_id[:128], arguments=dict(raw_args),
                   subject_ref=str(subject) if subject is not None else None)

    @property
    def digest(self) -> str:
        return action_digest(self.binding_id, self.arguments, self.subject_ref)


@dataclass(frozen=True)
class ActionStrategy:
    """Host-owned bounded recommendation over already-authorized actions."""
    eligible_actions: tuple[str, ...]
    priorities: Mapping[str, int]
    reason: str
    policy_hash: str

    def __post_init__(self) -> None:
        if not self.eligible_actions or len(set(self.eligible_actions)) != len(self.eligible_actions):
            raise ValueError("action strategy requires a unique non-empty eligible action set")
        if any(not isinstance(action, str) or not action for action in self.eligible_actions):
            raise ValueError("action strategy actions must be non-empty strings")
        if any(action not in self.eligible_actions or int(priority) < 0
               for action, priority in self.priorities.items()):
            raise ValueError("action strategy priorities must reference eligible actions")
        if not self.reason or not self.policy_hash:
            raise ValueError("action strategy requires a reason and policy hash")

    def to_dict(self) -> Dict[str, Any]:
        return {"eligible_actions": list(self.eligible_actions),
                "priorities": dict(sorted(self.priorities.items())),
                "reason": self.reason, "policy_hash": self.policy_hash}


@dataclass(frozen=True)
class ConfirmationGrant:
    grant_id: str
    actor_id: str
    action_digest: str
    scope: str
    issued_at: str
    expires_at: str
    task_id: Optional[str] = None
    policy_hash: Optional[str] = None
    session_nonce: Optional[str] = None
    signature: str = ""

    @classmethod
    def create(cls, *, grant_id: str, actor_id: str, action_digest: str,
               scope: str, issued_at: str, expires_at: str,
               task_id: str, policy_hash: str, session_nonce: str) -> "ConfirmationGrant":
        grant = cls(grant_id, actor_id, action_digest, scope, issued_at, expires_at,
                    task_id, policy_hash, session_nonce)
        return cls(grant_id, actor_id, action_digest, scope, issued_at, expires_at,
                   task_id, policy_hash, session_nonce, grant._signature())

    def _signature(self) -> str:
        payload = _canonical({
            "grant_id": self.grant_id, "actor_id": self.actor_id,
            "action_digest": self.action_digest, "scope": self.scope,
            "issued_at": self.issued_at, "expires_at": self.expires_at,
            "task_id": self.task_id, "policy_hash": self.policy_hash,
            "session_nonce": self.session_nonce,
        }).encode("utf-8")
        return hmac.new(_GRANT_SECRET, payload, hashlib.sha256).hexdigest()

    def is_valid_for(self, request: AgentActionRequest, *, principal: str,
                     task_scope: str, task_id: Optional[str] = None,
                     policy_hash: Optional[str] = None,
                     session_nonce: Optional[str] = None,
                     now: Optional[datetime] = None) -> bool:
        if self.actor_id != principal or self.scope not in {task_scope, "task", "*"}:
            return False
        if (self.task_id, self.policy_hash, self.session_nonce) != (
                task_id, policy_hash, session_nonce):
            return False
        if self.action_digest != request.digest:
            return False
        if not self.signature or not hmac.compare_digest(self.signature, self._signature()):
            return False
        instant = now or datetime.now(timezone.utc)
        try:
            issued = datetime.fromisoformat(self.issued_at.replace("Z", "+00:00"))
            expiry = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        return issued <= instant <= expiry


@dataclass(frozen=True)
class HostExecutionContext:
    principal: str = "runtime"
    task_scope: str = "task"
    confirmation_grants: tuple[ConfirmationGrant, ...] = ()
    capabilities: tuple[str, ...] = ()
    filesystem_scope: tuple[str, ...] = ()
    protected_resources: ProtectedResourcePolicy = None  # type: ignore[assignment]
    task_id: Optional[str] = None
    policy_hash: Optional[str] = None
    session_nonce: Optional[str] = None

    def __post_init__(self) -> None:
        if self.protected_resources is None:
            object.__setattr__(self, "protected_resources", ProtectedResourcePolicy.default())
        if not self.filesystem_scope:
            object.__setattr__(self, "filesystem_scope",
                               tuple(self.protected_resources.authorized_write_roots))

    def confirmation_for(self, request: AgentActionRequest, consumed: set[str],
                         *, now: Optional[datetime] = None) -> Optional[ConfirmationGrant]:
        for grant in self.confirmation_grants:
            if grant.grant_id in consumed:
                continue
            if grant.is_valid_for(request, principal=self.principal,
                                  task_scope=self.task_scope, task_id=self.task_id,
                                  policy_hash=self.policy_hash,
                                  session_nonce=self.session_nonce, now=now):
                return grant
        return None

    def has_confirmation(self, request: AgentActionRequest, *, now: Optional[datetime] = None) -> bool:
        return self.confirmation_for(request, set(), now=now) is not None
