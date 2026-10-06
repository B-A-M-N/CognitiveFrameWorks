# Adapter Generation Source

Adapter files under `*/adapters/` are generated artifacts.

## Source of truth

Each skill's canonical adapter content lives in:

```text
<Skill>/adapters/CLAUDE.md
```

## Generation rules

- `CLAUDE.md`, `.cursorrules`, `.windsurfrules`: copy canonical content.
- `system-prompt.md`: prepend `# System Prompt`.
- `.aider.conf.yml`: wrap canonical content under `system-prompt: |`.
- `continue-config.yaml`: wrap canonical content under `systemMessage: |`.

## Skills with adapters

- OWL
- ANCHOR
- DOX
- FUSE
- FLOW
- WARD
- SISPIS

Generated adapter files must not be edited directly. Update the canonical `CLAUDE.md` for the skill, then regenerate.
