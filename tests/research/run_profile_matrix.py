#!/usr/bin/env python3
"""Render and execute a BlueSky scenario through research plugin profiles."""

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

import numpy as np

from tests.research.matrix_preflight import parse_scenario, scenario_contract, validate_resources
from tests.research.run_manifest import SCHEMA_VERSION as MANIFEST_SCHEMA, validate_manifest
from tests.research.scenario_profiles import load_profile_config, render_scenario


ROOT = Path(__file__).resolve().parents[2]
SAMPLE_FIELDS = (
    "lat", "lon", "alt", "pressure_alt", "tas", "cas", "M", "vs", "hdg", "trk",
    "p", "rho", "Temp", "windnorth", "windeast", "atmos_source", "atmos_valid",
    "atmos_dataset_time", "atmos_fallback_reason",
)
PERFORMANCE_FIELDS = ("mass", "thrust", "drag", "fuelflow", "phase")
RECORDER_FIELDS = {
    "lat_deg": "lat", "lon_deg": "lon", "geometric_alt_m": "alt",
    "pressure_alt_m": "pressure_alt", "tas_m_s": "tas", "cas_m_s": "cas",
    "mach": "M", "vertical_speed_m_s": "vs", "heading_deg": "hdg",
    "track_deg": "trk", "temperature_k": "Temp", "pressure_pa": "p",
    "density_kg_m3": "rho", "wind_north_m_s": "windnorth",
    "wind_east_m_s": "windeast", "mass_kg": "mass", "thrust_n": "thrust",
    "drag_n": "drag", "fuel_flow_kg_s": "fuelflow",
}


def json_checksum(value):
    content = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def git_revision():
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=ROOT, check=True, text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
    return {
        "commit": git("rev-parse", "HEAD"),
        "upstream_base": git("rev-parse", "upstream/master"),
        "working_tree_dirty": bool(git("status", "--porcelain")),
    }


def manifest_for(profile_name, profile, contract, rendering, evidence, config_path):
    performance = dict(profile["performance"])
    if performance["provider"] == "PYBADA":
        performance["aircraft"] = sorted(
            {item["aircraft_type"] for item in rendering["replacements"]}
        )
    atmosphere = dict(profile["atmosphere"])
    recorder = dict(profile["recorder"])
    resources = {}
    if performance["provider"] == "PYBADA":
        resources[performance["dataset_id"]] = {
            "kind": "licensed-bada", "path": "configured by BlueSky settings"
        }
    if atmosphere["provider"] != "ISA":
        resources[atmosphere["dataset_id"]] = {
            "kind": "weather-cache", "path": evidence["resources"]["weather_cache"]
        }
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "revision": git_revision(),
        "experiment": {
            "scenario": rendering["source"],
            "rendered_scenario": rendering["rendered"],
            "source_sha256": rendering["source_sha256"],
            "rendered_sha256": rendering["rendered_sha256"],
            "profile_config": str(Path(config_path).resolve()),
            "simulation_utc": contract["simulation_utc"].isoformat(),
            "timestep_s": evidence["simulation"]["timestep_s"],
            "duration_s": evidence["simulation"]["simulated_duration_s"],
            "random_seed": 0,
        },
        "configuration": {
            "profile": profile_name, "performance": performance,
            "atmosphere": atmosphere, "recorder": recorder,
        },
        "external_resources": resources,
        "evidence": {
            "status": "valid" if evidence["status"] == "valid" else "invalid",
            "validators": ["tests/research/run_profile_matrix.py"],
        },
        "timing": evidence["timing"],
    }
    validate_manifest(manifest)
    return manifest


def scalar(values, index):
    value = np.asarray(values)[index]
    return value.item() if hasattr(value, "item") else value


def external_sample(step):
    import bluesky as bs
    rows = []
    for index, acid in enumerate(bs.traf.id):
        row = {"step": step, "sim_time_s": bs.sim.simt, "acid": acid,
               "actype": bs.traf.type[index]}
        row.update({name: scalar(getattr(bs.traf, name), index) for name in SAMPLE_FIELDS})
        for name in PERFORMANCE_FIELDS:
            values = getattr(bs.traf.perf, name, ())
            row[name] = None if index >= len(values) else scalar(values, index)
        rows.append(row)
    return rows


def require_plugin(name):
    from bluesky.core.plugin import Plugin
    success, message = Plugin.load(name)
    if not success:
        raise RuntimeError(message)


def validate_recorder_samples(csv_path, samples):
    external = {(float(row["sim_time_s"]), row["acid"]): row for row in samples}
    with Path(csv_path).open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    for index, row in enumerate(rows):
        key = (float(row["sim_time_s"]), row["acid"])
        sample = external.get(key)
        if sample is None:
            raise RuntimeError(f"Recorder row {index} has no matching external sample")
        for recorder_field, external_field in RECORDER_FIELDS.items():
            if row[recorder_field] == "" and sample[external_field] is None:
                continue
            if float(row[recorder_field]) != float(sample[external_field]):
                raise RuntimeError(
                    f"Recorder row {index} field {recorder_field} differs from external state"
                )
    return len(rows)


def compare_profiles(evidence_by_profile):
    comparisons = {}
    off = evidence_by_profile.get("baseline-recorder-free")
    on = evidence_by_profile.get("baseline-recorder")
    if off and on:
        left = off["external_samples"]["samples"]
        right = on["external_samples"]["samples"]
        exact = left == right
        off_sim = off["timing"]["simulation_wall_s"]
        on_sim = on["timing"]["simulation_wall_s"]
        comparisons["recorder_non_interference"] = {
            "status": "pass" if exact else "fail", "exact": exact,
            "recorder_simulation_overhead_s": on_sim - off_sim,
            "recorder_simulation_overhead_percent": (on_sim / off_sim - 1.0) * 100.0,
        }
    baseline = off or on
    if baseline:
        baseline_duration = baseline["simulation"]["simulated_duration_s"]
        baseline_wall = baseline["timing"]["simulation_wall_s"]
        for name, evidence in evidence_by_profile.items():
            if evidence is baseline:
                continue
            comparisons[f"{name}_vs_baseline"] = {
                "completion_time_difference_s":
                    evidence["simulation"]["simulated_duration_s"] - baseline_duration,
                "simulation_wall_time_difference_s":
                    evidence["timing"]["simulation_wall_s"] - baseline_wall,
                "simulation_runtime_ratio":
                    evidence["timing"]["simulation_wall_s"] / baseline_wall,
            }
    return comparisons


def configure_worker(profile, run_dir, weather_cache):
    import bluesky as bs
    performance = profile["performance"]
    atmosphere = profile["atmosphere"]
    recorder = profile["recorder"]
    weather = None
    if performance["provider"] == "PYBADA":
        require_plugin("PYBADATEM")
        from bluesky.plugins import pybada_tem
        success, message = pybada_tem.perfmodel(f"BADA{performance['family']}")
        if not success:
            raise RuntimeError(message)
    if atmosphere["provider"] != "ISA":
        from bluesky.core.entity import getproxied
        from bluesky.traffic.windsim import WindSim
        provider = atmosphere["provider"]
        bs.settings.meteo_strict = bool(atmosphere["strict"])
        bs.settings.meteo_time_autoupdate = bool(atmosphere["time_autoupdate"])
        bs.settings.meteo_time_interpolation = bool(atmosphere["interpolation"])
        bs.settings.era5_cache_path = str(Path(weather_cache) / "era5")
        bs.settings.gfs_cache_path = str(Path(weather_cache) / "gfs")
        require_plugin({"ERA5": "WINDECMWF", "GFS": "WINDGFS"}[provider])
        weather = getproxied(WindSim.instance())
        weather.strict = bool(atmosphere["strict"])
        success, message = weather.load(*atmosphere["bounds"])
        if not success:
            raise RuntimeError(message)
    recorder_module = None
    if recorder["enabled"]:
        bs.settings.research_output_path = str(run_dir)
        require_plugin("RESEARCHRECORDER")
        from bluesky.plugins import research_recorder as recorder_module
        success, message = recorder_module.record("INTERVAL", str(recorder["interval_s"]))
        if not success:
            raise RuntimeError(message)
        success, message = recorder_module.record("START", "samples.csv")
        if not success:
            raise RuntimeError(message)
    return weather, recorder_module


def run_worker(args):
    import bluesky as bs
    from bluesky.core import simtime

    process_started = time.process_time()
    wall_started = time.perf_counter()
    started_utc = dt.datetime.now(dt.timezone.utc)
    config = load_profile_config(args.config)
    profile = config["profiles"][args.profile]
    contract = scenario_contract(args.scenario)
    run_dir = args.output.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    initialization_started = time.perf_counter()
    args.workdir.mkdir(parents=True, exist_ok=True)
    bs.init(mode="sim", detached=True, workdir=args.workdir,
            configfile=args.bluesky_config)
    timestep = float(args.timestep)
    simtime.setdt(timestep)
    bs.sim.simdt = timestep
    bs.sim.utc = contract["simulation_utc"].replace(tzinfo=None)
    initialization_s = time.perf_counter() - initialization_started

    setup_started = time.perf_counter()
    weather, recorder_module = configure_worker(profile, run_dir, args.weather_cache)
    commands = [(when, command) for when, command, _ in parse_scenario(args.scenario)
                if not command.upper().startswith("DATE ")]
    bs.stack.set_scendata(*map(list, zip(*commands)))
    setup_s = time.perf_counter() - setup_started

    samples = []
    simulation_started = time.perf_counter()
    step = 0
    while bs.sim.state != bs.HOLD:
        bs.sim.step()
        samples.extend(external_sample(step))
        step += 1
        if step > int(contract["safety_duration_s"] / timestep) + 2:
            raise RuntimeError("Scenario exceeded its safety HOLD")
    simulation_wall_s = time.perf_counter() - simulation_started
    termination = "safety_hold" if bs.sim.simt >= contract["safety_duration_s"] - timestep \
        else "destination_reached"
    quality = "VALID"
    recorder_evidence = None
    if recorder_module is not None:
        quality = recorder_module.recorder.quality_status
        paths = recorder_module.recorder.stop()
        if paths:
            matched_rows = validate_recorder_samples(paths[0], samples)
            recorder_evidence = {
                "csv": str(paths[0]), "metadata": str(paths[1]),
                "events": str(recorder_module.recorder.event_path),
                "rows": recorder_module.recorder.rows,
                "common_samples_exact": matched_rows == recorder_module.recorder.rows,
            }
    if quality == "ABORTED":
        termination = "quality_abort"
    finished_utc = dt.datetime.now(dt.timezone.utc)
    wall_time_s = time.perf_counter() - wall_started
    evidence = {
        "schema_version": "profile-evidence-v1",
        "status": "valid" if termination == "destination_reached" and quality != "ABORTED"
                  else "invalid",
        "profile": args.profile,
        "simulation": {"timestep_s": timestep, "steps": step,
                       "simulated_duration_s": bs.sim.simt,
                       "termination_reason": termination},
        "timing": {
            "started_utc": started_utc.isoformat(), "finished_utc": finished_utc.isoformat(),
            "wall_time_s": wall_time_s, "cpu_time_s": time.process_time() - process_started,
            "initialization_wall_s": initialization_s, "plugin_setup_wall_s": setup_s,
            "simulation_wall_s": simulation_wall_s,
            "simulation_speed_ratio": bs.sim.simt / simulation_wall_s,
        },
        "external_samples": {"count": len(samples), "sha256": json_checksum(samples),
                             "samples": samples},
        "recorder": recorder_evidence,
        "weather": None if weather is None else {
            "active_slot": weather.active_slot, "failure_counts": weather.failure_counts,
        },
        "resources": {"weather_cache": str(Path(args.weather_cache).resolve())},
        "platform": {"python": platform.python_version(), "platform": platform.platform()},
    }
    args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    return 0 if evidence["status"] == "valid" else 2


def orchestrate(args):
    started = time.perf_counter()
    config = load_profile_config(args.config)
    contract = scenario_contract(args.scenario)
    selected = args.profiles or list(config["profiles"])
    unknown = sorted(set(selected) - set(config["profiles"]))
    if unknown:
        raise SystemExit(f"Unknown profiles: {', '.join(unknown)}")
    missing = validate_resources(contract, {"profiles": {
        name: config["profiles"][name] for name in selected
    }}, args.weather_cache)
    if missing and not args.allow_missing_weather:
        details = "\n".join(f"  {profile}: {path}" for profile, path in missing)
        raise SystemExit(f"Missing required weather cache files:\n{details}")

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    plan = {"scenario": str(args.scenario.resolve()), "profiles": {},
            "missing_weather": missing}
    for name in selected:
        run_dir = output / name
        rendered = run_dir / "scenario.scn"
        rendering = render_scenario(args.scenario, rendered, config, name)
        plan["profiles"][name] = rendering
    (output / "plan.json").write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n",
                                      encoding="utf-8")
    if args.preflight_only:
        print(f"PREFLIGHT PASSED: {len(selected)} profiles rendered in {output}")
        return 0

    results = {}
    evidence_by_profile = {}
    for name in selected:
        run_dir = output / name
        evidence_path = run_dir / "evidence.json"
        workdir = run_dir / "work"
        env = os.environ.copy()
        env.update({"PYTHONNOUSERSITE": "1", "PYTHONPATH": str(ROOT),
                    "MPLCONFIGDIR": str(run_dir / "mpl")})
        command = [sys.executable, str(Path(__file__).resolve()), "--worker",
                   "--scenario", str(run_dir / "scenario.scn"), "--config", str(args.config),
                   "--profile", name, "--output", str(run_dir), "--workdir", str(workdir),
                   "--evidence", str(evidence_path), "--weather-cache", str(args.weather_cache),
                   "--timestep", str(args.timestep)]
        if args.bluesky_config:
            command.extend(("--bluesky-config", str(args.bluesky_config)))
        completed = subprocess.run(command, cwd=ROOT, env=env)
        if not evidence_path.is_file():
            results[name] = {
                "returncode": completed.returncode,
                "status": "invalid",
                "error": "worker exited without producing evidence",
            }
            break
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence_by_profile[name] = evidence
        manifest = manifest_for(name, config["profiles"][name], contract,
                                plan["profiles"][name], evidence, args.config)
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        results[name] = {"returncode": completed.returncode,
                         "status": evidence["status"], "timing": evidence["timing"]}
        if completed.returncode:
            break
    comparisons = compare_profiles(evidence_by_profile)
    summary = {"schema_version": "profile-matrix-v1", "results": results,
               "comparisons": comparisons,
               "orchestration_wall_s": time.perf_counter() - started}
    (output / "matrix-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    comparisons_pass = all(value.get("status", "pass") == "pass"
                           for value in comparisons.values())
    return 0 if len(results) == len(selected) and comparisons_pass and all(
        result["status"] == "valid" for result in results.values()) else 2


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    command.add_argument("--scenario", type=Path, required=True)
    command.add_argument("--config", type=Path, default=ROOT / "experiments/profiles.json")
    command.add_argument("--profile", help=argparse.SUPPRESS)
    command.add_argument("--profiles", nargs="+")
    command.add_argument("--output", type=Path, required=True)
    command.add_argument("--workdir", type=Path, help=argparse.SUPPRESS)
    command.add_argument("--evidence", type=Path, help=argparse.SUPPRESS)
    command.add_argument("--weather-cache", type=Path, default=ROOT / "cache/weather")
    command.add_argument("--bluesky-config", type=Path, default=ROOT / "settings.cfg")
    command.add_argument("--timestep", type=float, default=0.5)
    command.add_argument("--preflight-only", action="store_true")
    command.add_argument("--allow-missing-weather", action="store_true")
    return command


def main():
    args = parser().parse_args()
    if args.worker:
        if not args.profile or not args.workdir or not args.evidence:
            raise SystemExit("Worker mode requires profile, workdir, and evidence")
        return run_worker(args)
    return orchestrate(args)


if __name__ == "__main__":
    raise SystemExit(main())
