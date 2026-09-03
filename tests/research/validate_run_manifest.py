"""Validate a research-run-v2 JSON document without external dependencies."""

import argparse
import json
from pathlib import Path

from tests.research.run_manifest import ManifestError, validate_manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        validate_manifest(json.loads(args.manifest.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ManifestError) as exc:
        raise SystemExit(f"INVALID research run manifest: {exc}") from exc
    print(f"VALID research run manifest: {args.manifest}")


if __name__ == "__main__":
    main()
