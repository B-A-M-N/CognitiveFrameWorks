# Cognitive Runtime security boundary

`RuntimeSession` is the agent-facing task object. It intentionally does not
expose a public host-ingress property: evidence attestations, operator
observations, validator results, and handoff publication enter through the
host adapter (`CognitiveRuntime` or a platform-specific interceptor).

The Python implementation is an in-process reference adapter, not a process
sandbox. A caller with arbitrary access to private Python attributes can
still bypass object visibility. Production integrations must keep the agent
and host adapter in separate trust domains (or use a narrow IPC/RPC bridge)
and must never serialize `_host_capability`, `HostIngress`, or
`HostExecutionContext` into model-visible state.

Host grants, evidence, packets, and completion permits are bound to task,
subject, policy, and session identity. The host remains responsible for
supplying actual tool/operator/validator facts.
