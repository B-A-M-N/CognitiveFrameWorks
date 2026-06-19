# Restraint Trigger — "Calling tools to appear thorough"

Calibrates: performative tool calls caught and suppressed. Restraint and Necessity both fire.

---

## Input

> What's the difference between `const` and `let` in JavaScript?

---

## Initial Impulse

The model considers calling:
1. `grep` for `const` in the project
2. `grep` for `let` in the project
3. `view` the README to see if it explains the project's style

---

## Strategy Pass

**Necessity:** The question is about a language-level distinction (`const` vs `let` in JavaScript), not about this project's usage. The answer is general knowledge already in the model's context. No external state needs to be observed.
Signal: `unnecessary_tool_call` (0.5) — the proposed grep calls would only reconfirm general knowledge, not produce new evidence.

**Selection:** Not applicable — no tool is the right tool here.
Signals: none.

**Sequencing:** Not applicable — no calls to sequence.
Signals: none.

**Concurrency:** Not applicable.
Signals: none.

**Resource Bounds:** Not applicable.
Signals: none.

**Evidence Interpretation:** Not applicable — no results to interpret.
Signals: none.

**Termination:** Not applicable.
Signals: none.

**Restraint:** The proposed calls (grep for `const`, grep for `let`, view README) would produce evidence about *this project's usage*, not about the *language distinction* the user asked about. The calls would appear thorough — "I checked the codebase!" — but would not answer the question better than general knowledge. This is performative tool use.
Signal: `performative_tool_call` (0.5) — calls being made to demonstrate activity rather than produce relevant evidence.

The README call is also `heavyweight_for_lightweight` (0.5) — reading the README to answer a language question is a mismatch between tool and need. But `performative_tool_call` already covers the core issue; adding `heavyweight_for_lightweight` would be redundant from the same observation. Suppress per stacking rules.

---

## Gate

W_fuse = 1.0 (0.5 + 0.5) < 1.5 → silent.

The signals don't surface, but the calls are suppressed internally. No tools are called.

---

## Output

`const` declares a variable that cannot be reassigned. `let` declares a variable that can be reassigned. Both are block-scoped.

```javascript
const x = 1;
x = 2; // TypeError: Assignment to constant variable.

let y = 1;
y = 2; // fine
```

Use `const` by default. Use `let` only when reassignment is needed.

---

## What This Calibrates

Restraint catches tool calls that would *appear* productive but wouldn't *be* productive. The distinction: a tool call is warranted when it produces evidence the answer depends on. A tool call is performative when it produces evidence tangential to the question, used to signal effort.

The proposed greps would have returned matches — `const` and `let` certainly appear in the project. But those matches would not have improved the answer about the language-level distinction. The calls would have cost latency and context for zero evidence gain.

Anti-pattern to avoid: "Let me search the codebase to give you a thorough answer..." followed by grep calls that return usage examples, then answering from general knowledge anyway. The calls added nothing. Answer directly.

---

## Necessity vs Restraint

This example fires both because the two principles overlap on this case:

| Principle | Question | Answer here |
|-----------|----------|-------------|
| Necessity | Is the answer already in context? | Yes — general knowledge. No tool needed. |
| Restraint | Is the tool call counterproductive even if it would work? | Yes — calls would produce irrelevant evidence. |

Necessity asks "do I need this?" Restraint asks "should I avoid this even though I could?" When both fire on the same call, the call is clearly wrong. When only one fires, the distinction matters: a call can be necessary (external state must be verified) but require restraint (use scoped read, not wholesale read). A call can be unnecessary (answer is in context) but not harmful (a quick confirmation). The principles are independent gates, not redundant ones.
