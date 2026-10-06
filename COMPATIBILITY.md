# Runtime compatibility

The beta runtime pins these contract families together:

| Component | Compatible line |
|---|---|
| CognitiveFrameWorks | 1.4.x |
| CognitiveStateWork protocol | 1.0.x |
| CFW runtime-event schema | 1.0.x |
| guard-pack schema | 1.1.x |
| DigitalPsychology registry/receipt schemas | 1.0.x |

The resolver embeds the exact schema and executable hashes in every frozen
policy. A major-version mismatch or changed enforcement registry invalidates
the policy instead of silently downgrading enforcement.
