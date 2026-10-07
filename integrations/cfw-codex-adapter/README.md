# CognitiveFrameWorks Codex adapter

This optional compatibility distribution preserves the historical CFW Codex
imports. The base `cognitive-frameworks-runtime` distribution does not
package or depend on Codex host modules.

The Codex transport and the CFW task host live in
`cognitive-runtime-codex-adapter`, under
`cognitive_runtime_core.integrations.codex`. The CFW `scripts/codex_host`
modules and top-level facades in this distribution are thin compatibility
imports. The task host uses CFW's local action, validator, and completion
authorities and is imported only when Codex is selected.

From a workspace containing adjacent DigitalPsychology and CFW checkouts:

```bash
python3 -m pip install ../DigitalPsychology/cognitive_runtime_core
python3 -m pip install .
python3 -m pip install '../DigitalPsychology/cognitive_runtime_core/integrations/codex[cfw]'
python3 -m pip install ./integrations/cfw-codex-adapter
```

The adapter distribution packages the compatibility modules from
`scripts/codex_host` and the two legacy top-level facades. The implementation
is packaged with the optional Codex integration, while the CFW checkout keeps
the compatibility import paths for existing hosts and integration tests.
