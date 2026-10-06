#!/usr/bin/env python3
"""Legacy CLI and import bridge for :mod:`resolver.api`."""
from __future__ import annotations

import sys
from resolver import api as _api


def __getattr__(name: str):
    return getattr(_api, name)


def main(argv: list[str] | None = None) -> int:
    return _api.main(argv or sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
