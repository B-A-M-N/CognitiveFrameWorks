"""Run one sanitized model-backed Codex cfw_read and DP boundary probe."""
from __future__ import annotations

import sys

from live_codex_app_server_coding_trial import main


if __name__ == "__main__":
    sys.argv[1:1] = ["--condition", "triad", "--dynamic-tool-probe"]
    raise SystemExit(main())
