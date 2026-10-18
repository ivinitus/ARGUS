"""Deprecated shim."""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from argus.operations import prune

    print("note: tools/prune_output.py is deprecated; use `argus prune`",
          file=sys.stderr)
    return prune.main()


if __name__ == "__main__":
    raise SystemExit(main())
