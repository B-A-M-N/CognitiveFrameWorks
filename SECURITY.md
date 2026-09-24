# Runtime security boundary

The Cognitive Runtime authority journal, packet store, evidence ledger, and
host ingress are protected by the host process and its same-user filesystem.
That host process plus the protected runtime directory is the trusted
computing base for this release. `AgentSessionHandle` and
`RuntimeTaskHandle` are narrow integration capabilities, but Python code that
already runs inside the host process is not treated as an adversary: Python
introspection can inspect module globals and call private objects.

Untrusted plugins or model-controlled code must run outside the host process
and communicate through an IPC/RPC gateway that authenticates requests and
does not expose the authority registry. Filesystem ownership and permissions
must prevent that untrusted process from modifying the runtime journal or
compiled policy artifacts. The SQLite journal is durable and replay-checked,
but it is not a cryptographic MAC against a same-user process that can write
the protected journal directory.
