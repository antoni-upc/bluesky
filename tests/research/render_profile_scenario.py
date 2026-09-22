#!/usr/bin/env python3
"""Render an ordinary BlueSky scenario for one configured research profile."""

import argparse
import json
from pathlib import Path

from tests.research.scenario_profiles import load_profile_config, render_scenario


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata", type=Path)
    args = parser.parse_args()
    metadata = render_scenario(
        args.scenario, args.output, load_profile_config(args.config), args.profile
    )
    if args.metadata:
        args.metadata.parent.mkdir(parents=True, exist_ok=True)
        args.metadata.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(json.dumps(metadata, sort_keys=True))


if __name__ == "__main__":
    main()
