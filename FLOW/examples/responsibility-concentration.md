# Responsibility Concentration — God Object, Single-Use Extraction, and Negative Control

Calibrates the inverse of unnecessary abstraction: too few responsibility boundaries. Expected signals are `responsibility_concentration` and, when unrelated changes fan out through the same coordinator, `change_amplification`.

---

## Positive Case: Central Object Grows Another Domain

### Input

> Add audit logging for every payment action. Keep it in `ApplicationManager` because that is where the payment, auth, and notification calls already live.

### Current shape (responsibility evidence, not LOC)

```python
class ApplicationManager:
    # Owns orchestration + authentication policy + database connections +
    # provider/API retries + notifications + payment-specific state.
    def login(self): ...
    def start_database(self): ...
    def call_payment_provider(self): ...
    def send_notification(self): ...
    def dispatch(self, command):
        if command.kind == "auth": ...
        elif command.kind == "payment": ...
        elif command.kind == "notification": ...
```

### FLOW pass

The edit introduces **audit policy and persistence** as a new reason to change. `ApplicationManager` already owns at least five unrelated domains, and future audit requirements would converge on the same coordinator. This is responsibility evidence; line count is not the trigger.

**Signals**
- `responsibility_concentration` (2.0): adding audit ownership to a component that already owns authentication, database lifecycle, payment provider interaction, notifications, and dispatch.
- `change_amplification` (1.0): audit rules will require touching payment, notification, and dispatch branches plus shared internal state in one class.

W_flow = 3.0 → Surface mode.

### Correct correction

```python
class AuditLog:          # owns audit format + persistence policy
    def record_payment_action(self, event): ...

class PaymentService:    # owns provider interaction + payment domain policy
    def __init__(self, provider, audit_log): ...
    def charge(self, request):
        result = self.provider.charge(request)
        self.audit_log.record_payment_action(result)
        return result

class ApplicationManager:  # thin coordinator / façade only
    def __init__(self, payment_service): ...
    def pay(self, request):
        return self.payment_service.charge(request)
```

Move the existing payment behavior across the boundary without semantic drift, then implement audit logging through `PaymentService`. The coordinator no longer owns provider mechanics, payment policy, or audit persistence.

---

## Valid Single-Use Responsibility Extraction

### Input

> Add encrypted receipt persistence. The orchestrator already performs business policy, SQL, filesystem writes, provider routing, and state mutation.

### Correct judgment

`ReceiptStore` may have only one caller, but it establishes ownership for storage format, encryption, file lifecycle, and failure behavior. This is **not premature abstraction** solely because it has one caller.

Expected reasoning: preserve behavior while extracting the side-effect boundary, keep the orchestrator thin, then add encrypted persistence through `ReceiptStore`. Do not demand a hypothetical second use case; reuse and cohesion are different concerns.

---

## Negative Control: Large But Cohesive

### Input

> This parser is 1,200 lines. Split it before adding a new production grammar rule.

```python
class ProductionParser:
    # Owns one domain: tokenization, grammar dispatch, AST construction,
    # and diagnostics for the same production grammar.
    def parse(self): ...
    def parse_expression(self): ...
    def parse_declaration(self): ...
    def report_syntax_error(self): ...
```

### Correct judgment

No FLOW structural signal. The component has one reason to change—the grammar—and its size follows from a necessarily detailed grammar. Splitting by line count would add indirection without clarifying ownership.

Add the new grammar rule to the relevant cohesive parsing area unless concrete evidence shows unrelated state, I/O, configuration, or subsystem policy hidden inside the parser.

---

## What This Calibrates

- A God Object is defined by mixed responsibilities and reasons to change, not by a line threshold.
- A single-use collaborator can be justified by ownership cohesion.
- A large cohesive component must not be split merely because it is large.
- Conservation applies to extraction: move existing behavior faithfully first, then implement the new behavior through the boundary.
