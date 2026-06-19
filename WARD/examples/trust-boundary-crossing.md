# Trust Boundary Crossing — "Check the user's global git config"

Calibrates: trust boundary discipline when an action crosses from project scope to user/private scope. WARD constrains by naming the boundary before crossing.

---

## Input

> Check if the user's git config has a global email set.

---

## Context

The project is at `/home/user/projects/myapp/`. The global git config is at `~/.gitconfig` (boundary level 2: user scope). The project's `.git/config` is at the project level (boundary level 1: local/project).

---

## FUSE Selection

FUSE selects: `view` on `~/.gitconfig`. Necessity: the file is external state not in context. Selection: view reads file contents. Sequencing: read before reporting. Single call.

---

## WARD Authority Pass (before execution)

**Authority Fit:** "Check if the user's git config has a global email set" — the user explicitly asked for their own global config. Authority is `granted`.
Signals: none.

**Blast Radius:** Read-only. No mutation. Blast radius is low.
Signals: none.

**Secret Hygiene:** `~/.gitconfig` may contain the user's email and name. These are personal data but not secrets in the credential sense. No `KEY`, `TOKEN`, or `PASSWORD` patterns expected.
Signals: none (email in git config is not a secret — it's user identity data).

**Trust Boundary Discipline:** The target `~/.gitconfig` is at boundary level 2 (user scope). The action crosses from the project scope (level 1) to the user's private config (level 2). This is a boundary crossing that must be acknowledged.
Signal: `boundary_crossing` (1.0, constrain) — crossing from project to user scope. Constrain: name the boundary before reading.

**Mutation Consent:** Read-only. No mutation.
Signals: none.

**Reversibility:** Read-only. No mutation.
Signals: none.

**Dependency & Supply-Chain Caution:** Not applicable.
Signals: none.

**Policy Preservation:** No DOX contracts loaded. No policy conflict.
Signals: none.

---

## Gate

Hard gate: `boundary_crossing` (constrain). The action is permitted but the boundary must be named.

W_ward = 1.0 < 1.5 → soft gate does not fire.

---

## Surface

```
**[WARD risk]:** Reading `~/.gitconfig` crosses from project scope (level 1) to user/private scope (level 2). The file is outside the project directory.
[Authority: granted — user asked for their own global config.]
[Blast radius: low — read-only, user's own data.]
[Required action: constrain — acknowledge boundary crossing before reading.]
```

---

## Constrained Action

The boundary is named explicitly, then the read proceeds:

```
Reading from user scope (~/.gitconfig) — crossing from project to user boundary.
```

```
view ~/.gitconfig
```

---

## Output

```
[user scope] ~/.gitconfig contains:
- user.name = "Jane Doe"
- user.email = "jane@example.com"

Global email is set.
```

---

## What This Calibrates

The boundary crossing is acknowledged before the read. The user can see that the agent accessed a file outside the project. This is not a refusal — the user asked for their own config — but the crossing is named so the user knows where the agent reached.

The distinction from Secret Hygiene: the user's email in `.gitconfig` is personal data, not a secret. WARD does not treat all personal data as secrets — it treats credentials, tokens, and keys as secrets. Personal data at boundary level 2 is a trust boundary concern, not a secret hygiene concern.

Anti-pattern: silently reading `~/.gitconfig` without acknowledging the boundary crossing. The user asked for it, so it's permitted — but the crossing should be visible.

---

## Counter-Example: Unscoped External Call

If the input were: "Check if the API is healthy" and the agent selects `curl https://api.example.com/health`:

This is a level 4 boundary crossing (external/network). The target `api.example.com` is not implied by the project — it's an external service. WARD fires `unscoped_external_call` (1.0, confirm): "This will make a network call to api.example.com. Confirm before executing?"

The user confirms → proceed. The boundary is named, consent is given, the call executes.

---

## ANCHOR Integration

| State | Value |
|-------|-------|
| Authority | granted — user asked for their own config |
| Boundary crossed | level 1 (project) → level 2 (user) |
| WARD decision | constrain — name boundary before reading |
| Action taken | view ~/.gitconfig with boundary acknowledgment |
| Data accessed | user.name, user.email (user's own data) |
