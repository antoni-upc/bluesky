"""Compare plugin-disabled OpenAP/ISA behavior with pinned upstream."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

os.environ.setdefault("MPLCONFIGDIR", "/tmp/bluesky-mpl-disabled-compare")

from tests.research.run_disabled_baseline import validate_result


PINNED_UPSTREAM = "22fdf9e3e77c077e0ddb5d7b14c70d67f9a5c855"
ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tests/research/run_disabled_baseline.py"


def command(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def regular_file_filter(member, destination):
    """Retain safe regular archive content; documentation links are unneeded."""
    if member.issym() or member.islnk():
        return None
    return tarfile.data_filter(member, destination)


def load_validated(path):
    return validate_result(json.loads(path.read_text(encoding="utf-8")))


def first_difference(left, right, path="$"):
    if type(left) is not type(right):
        return f"{path}: type {type(left).__name__} != {type(right).__name__}"
    if isinstance(left, dict):
        if left.keys() != right.keys():
            return f"{path}: keys {list(left)} != {list(right)}"
        for key in left:
            difference = first_difference(left[key], right[key], f"{path}.{key}")
            if difference:
                return difference
        return None
    if isinstance(left, list):
        if len(left) != len(right):
            return f"{path}: lengths {len(left)} != {len(right)}"
        for idx, (left_item, right_item) in enumerate(zip(left, right)):
            difference = first_difference(left_item, right_item, f"{path}[{idx}]")
            if difference:
                return difference
        return None
    return None if left == right else f"{path}: {left!r} != {right!r}"


def run_checkout(checkout, runner, workdir, output):
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONPATH"] = str(checkout)
    env["MPLCONFIGDIR"] = str(workdir.parent / f"mpl-{workdir.name}")
    command([
        sys.executable, str(runner), "--workdir", str(workdir),
        "--output", str(output),
    ], cwd=checkout, env=env)


def compare():
    with tempfile.TemporaryDirectory(prefix="bluesky-disabled-") as temp_name:
        temp = Path(temp_name)
        archive = temp / "upstream.tar"
        upstream = temp / "upstream"
        upstream.mkdir()
        with archive.open("wb") as stream:
            command(["git", "archive", PINNED_UPSTREAM], cwd=ROOT, stdout=stream)
        with tarfile.open(archive) as bundle:
            bundle.extractall(upstream, filter=regular_file_filter)
        upstream_runner = upstream / "run_disabled_baseline.py"
        shutil.copy2(RUNNER, upstream_runner)

        upstream_output = temp / "upstream.json"
        integration_output = temp / "integration.json"
        run_checkout(upstream, upstream_runner, temp / "upstream-work", upstream_output)
        run_checkout(ROOT, RUNNER, temp / "integration-work", integration_output)

        upstream_result = load_validated(upstream_output)
        integration_result = load_validated(integration_output)
        difference = first_difference(upstream_result, integration_result)
        if difference:
            raise RuntimeError(f"Plugin-disabled results differ: {difference}")

        upstream_bytes = upstream_output.read_bytes()
        integration_bytes = integration_output.read_bytes()
        if upstream_bytes != integration_bytes:
            raise RuntimeError("Validated JSON values match but serialized bytes differ")
        checksum = hashlib.sha256(upstream_bytes).hexdigest()
        print(f"UPSTREAM {PINNED_UPSTREAM} SHA-256 {checksum}")
        print(f"INTEGRATION SHA-256 {checksum}")
        print("PLUGIN-DISABLED OPENAP/ISA GATE PASSED: byte-identical JSON")


def main():
    parser = argparse.ArgumentParser()
    parser.parse_args()
    compare()


if __name__ == "__main__":
    main()
