"""Run the deterministic plugin-disabled OpenAP/ISA comparison.

This script is intentionally compatible with both upstream/master and the
research integration branch. Run it from either checkout and compare the JSON
documents it writes.
"""

import argparse
import datetime as dt
import json
from pathlib import Path

import numpy as np

import bluesky as bs
from bluesky.core import simtime
from bluesky.tools.aero import ft, kts, vatmos
from bluesky.traffic.performance.perfbase import PerfBase


UTC = dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.timezone.utc)
SIMDT_S = 0.5
STEPS = 240


def scalar(values, idx=0):
    """Return a JSON-native scalar from a BlueSky traffic array."""
    return np.asarray(values)[idx].item()


def run(workdir):
    bs.init(mode="sim", detached=True, workdir=workdir)
    simtime.setdt(SIMDT_S)
    bs.sim.simdt = SIMDT_S
    bs.sim.utc = UTC

    created = bs.traf.cre(
        "BASE1", "A320", 41.30, 2.10, 73.0, 10_000.0 * ft, 250.0 * kts
    )
    if created is not True and (not created or created[0] is not True):
        raise RuntimeError(f"Aircraft creation failed: {created}")

    # Fixed targets exercise native acceleration, turn, and climb logic while
    # all optional research plugins remain unloaded.
    bs.traf.ap.selspdcmd(0, 280.0 * kts)
    bs.traf.ap.selhdgcmd(0, 101.0)
    bs.traf.ap.selaltcmd(0, 12_000.0 * ft, 1_500.0 * ft / 60.0)

    for _ in range(STEPS):
        bs.sim.step()

    isa_p, isa_rho, isa_temp = vatmos(bs.traf.alt)
    state = {
        "lat_deg": scalar(bs.traf.lat),
        "lon_deg": scalar(bs.traf.lon),
        "alt_m": scalar(bs.traf.alt),
        "tas_m_s": scalar(bs.traf.tas),
        "cas_m_s": scalar(bs.traf.cas),
        "mach": scalar(bs.traf.M),
        "vertical_speed_m_s": scalar(bs.traf.vs),
        "heading_deg": scalar(bs.traf.hdg),
        "track_deg": scalar(bs.traf.trk),
        "pressure_pa": scalar(bs.traf.p),
        "density_kg_m3": scalar(bs.traf.rho),
        "temperature_k": scalar(bs.traf.Temp),
        "mass_kg": scalar(bs.traf.perf.mass),
        "thrust_n": scalar(bs.traf.perf.thrust),
        "drag_n": scalar(bs.traf.perf.drag),
        "fuel_flow_kg_s": scalar(bs.traf.perf.fuelflow),
        "phase": scalar(bs.traf.perf.phase),
    }
    return {
        "inputs": {
            "simulation_utc": UTC.isoformat(),
            "simdt_s": SIMDT_S,
            "steps": STEPS,
            "duration_s": SIMDT_S * STEPS,
            "aircraft": "A320",
            "research_plugins_loaded": [],
            "performance_model": PerfBase.selected().__name__,
            "atmosphere": "ISA",
        },
        "state": state,
        "isa_reference": {
            "pressure_pa": scalar(isa_p),
            "density_kg_m3": scalar(isa_rho),
            "temperature_k": scalar(isa_temp),
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.workdir)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
