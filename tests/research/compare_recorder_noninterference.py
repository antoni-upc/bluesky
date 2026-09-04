#!/usr/bin/env python3
"""Prove that RESEARCHRECORDER does not alter an OpenAP/ISA trajectory."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import numpy as np

import bluesky as bs
from bluesky.core import simtime
from bluesky.tools.aero import ft, kts


ROOT = Path(__file__).resolve().parents[2]
UTC = "2026-08-05T12:00:00+00:00"
SIMDT_S = 0.5
STEPS = 240
SAMPLE_FIELDS = (
    "lat", "lon", "alt", "tas", "cas", "M", "vs", "hdg", "trk",
    "p", "rho", "Temp", "windnorth", "windeast",
)
PERFORMANCE_FIELDS = ("mass", "thrust", "drag", "fuelflow", "phase")


def scalar(values):
    return np.asarray(values)[0].item()


def external_sample(step):
    sample = {"step": step, "sim_time_s": step * SIMDT_S}
    sample.update({name: scalar(getattr(bs.traf, name)) for name in SAMPLE_FIELDS})
    sample.update({name: scalar(getattr(bs.traf.perf, name))
                   for name in PERFORMANCE_FIELDS})
    return sample


def run_child(workdir, output, recorder_enabled):
    import datetime as dt

    workdir.mkdir(parents=True, exist_ok=True)
    bs.init(mode="sim", detached=True, workdir=workdir)
    simtime.setdt(SIMDT_S)
    bs.sim.simdt = SIMDT_S
    bs.sim.utc = dt.datetime.fromisoformat(UTC)

    recorder_module = None
    if recorder_enabled:
        from bluesky.core.plugin import Plugin

        success, message = Plugin.load("RESEARCHRECORDER")
        if not success:
            raise RuntimeError(message)
        from bluesky.plugins import research_recorder as recorder_module
        success, message = recorder_module.record("INTERVAL", str(SIMDT_S))
        if not success:
            raise RuntimeError(message)
        success, message = recorder_module.record("START", "baseline-recorder.csv")
        if not success:
            raise RuntimeError(message)

    created = bs.traf.cre(
        "BASE1", "A320", 41.30, 2.10, 73.0, 10_000.0 * ft, 250.0 * kts
    )
    if created is not True and (not created or created[0] is not True):
        raise RuntimeError(f"Aircraft creation failed: {created}")
    bs.traf.ap.selspdcmd(0, 280.0 * kts)
    bs.traf.ap.selhdgcmd(0, 101.0)
    bs.traf.ap.selaltcmd(0, 12_000.0 * ft, 1_500.0 * ft / 60.0)

    samples = [external_sample(0)]
    for step in range(1, STEPS + 1):
        bs.sim.step()
        samples.append(external_sample(step))

    recorder_evidence = None
    if recorder_enabled:
        paths = recorder_module.recorder.stop()
        if paths is None:
            raise RuntimeError("Recorder did not finalize evidence")
        recorder_evidence = {
            "csv": paths[0].name,
            "metadata": paths[1].name,
            "rows": recorder_module.recorder.rows,
        }
    output.write_text(json.dumps({
        "recorder_enabled": recorder_enabled,
        "experiment": {"simulation_utc": UTC, "simdt_s": SIMDT_S, "steps": STEPS},
        "samples": samples,
        "recorder_evidence": recorder_evidence,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def first_difference(left, right, path="samples"):
    if type(left) is not type(right):
        return f"{path}: types differ"
    if isinstance(left, dict):
        if left.keys() != right.keys():
            return f"{path}: fields differ"
        for key in left:
            difference = first_difference(left[key], right[key], f"{path}.{key}")
            if difference:
                return difference
        return None
    if isinstance(left, list):
        if len(left) != len(right):
            return f"{path}: lengths {len(left)} != {len(right)}"
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            difference = first_difference(left_item, right_item, f"{path}[{index}]")
            if difference:
                return difference
        return None
    return None if left == right else f"{path}: {left!r} != {right!r}"


def compare():
    with tempfile.TemporaryDirectory(prefix="bluesky-recorder-compare-") as name:
        temp = Path(name)
        outputs = {}
        for label in ("off", "on"):
            output = temp / f"{label}.json"
            env = os.environ.copy()
            env.update({
                "PYTHONNOUSERSITE": "1", "PYTHONPATH": str(ROOT),
                "MPLCONFIGDIR": str(temp / f"mpl-{label}"),
            })
            subprocess.run([
                sys.executable, str(Path(__file__).resolve()), "--child",
                "--recorder", label, "--workdir", str(temp / f"work-{label}"),
                "--output", str(output),
            ], cwd=ROOT, env=env, check=True)
            outputs[label] = json.loads(output.read_text(encoding="utf-8"))

        difference = first_difference(outputs["off"]["samples"], outputs["on"]["samples"])
        if difference:
            raise RuntimeError(f"Recorder changed externally sampled state: {difference}")
        if outputs["on"]["recorder_evidence"]["rows"] <= 0:
            raise RuntimeError("Recorder-enabled run produced no recorder rows")
        encoded = json.dumps(outputs["off"]["samples"], sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        checksum = hashlib.sha256(encoded).hexdigest()
        print(f"EXTERNAL SAMPLES: {len(outputs['off']['samples'])}, SHA-256 {checksum}")
        print(f"RECORDER ROWS: {outputs['on']['recorder_evidence']['rows']}")
        print("RECORDER NON-INTERFERENCE GATE PASSED: external samples are identical")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--recorder", choices=("off", "on"), help=argparse.SUPPRESS)
    parser.add_argument("--workdir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        if args.recorder is None or args.workdir is None or args.output is None:
            parser.error("child mode requires recorder, workdir, and output")
        run_child(args.workdir, args.output, args.recorder == "on")
    else:
        compare()


if __name__ == "__main__":
    main()
