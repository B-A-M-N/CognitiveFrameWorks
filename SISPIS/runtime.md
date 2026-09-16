# SISPIS — Runtime Capsule

Final response calibration. Runs last, always, on the merged semantic
envelope from all upstream stages.

## Merge (deterministic; software, not model arithmetic)
1. Group all envelopes by cause_id.
2. Dedupe exact signal_type duplicates within a cause.
3. Per cause, per entropy dimension: take the max effect among signals;
   intent weight: max; output floor: strongest floor.
4. Sum across independent causes.

## Calibration
Look up each signal_type in SISPIS/references/signal-calibration.yaml
(single source of truth; upstream skills never compute these). required_action
floors apply from calibration (confirm/refuse/recover/reclassify →
explanation).

## Gate
Choose response structure (direct / explanation / options / schema) from the
calibrated entropy + intent + floor, then output. Never fabricate signal
values; unknown signal types contribute nothing.
