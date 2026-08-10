#!/usr/bin/env python3
"""Audit or merge duplicate company rows using the canonical data-store rules."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.data_store import deduplicate_companies  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply merges; without this flag only print the plan",
    )
    args = parser.parse_args()
    result = deduplicate_companies(dry_run=not args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
