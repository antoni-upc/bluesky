"""
run_fast.py - Run a BlueSky scenario in headless fast-time without a GUI.

Usage:
    python run_fast.py scenario/TFG1_GFS.scn
    python run_fast.py scenario/TFG1_GFS.scn --ff 3600   # run for 1 hour sim-time
"""
import sys
import os
import argparse

# Ensure bluesky modules can be imported from the repo root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bluesky as bs
from bluesky.core.walltime import Timer


def main():
    parser = argparse.ArgumentParser(
        description="Run BlueSky scenarios headlessly in fast-time."
    )
    parser.add_argument("scenario", help="Path to the scenario file (.scn)")
    parser.add_argument(
        "--ff", dest="fastforward", type=float, default=None,
        help="Simulation time in seconds to advance fast-forward. "
             "If not given, runs until scenario's STOP command or sim end."
    )
    args = parser.parse_args()

    scenario_file = args.scenario
    if not os.path.isfile(scenario_file):
        print(f"Error: Scenario file '{scenario_file}' not found.")
        sys.exit(1)

    print(f"Initializing BlueSky for scenario: {scenario_file}")

    # --- Init BlueSky in headless simulation mode (in-process, no GUI) ---
    # NOTE: detached=True must NOT be used here – it spawns a background
    # simulation node that re-loads navdata and then hangs indefinitely.
    bs.init(mode='sim', detached=False)

    # Queue the scenario file on the stack using IC
    # IC prepends settings.scenario_path, so pass just the basename
    bs.stack.stack(f"IC {os.path.basename(scenario_file)}")

    # Run the sim in full fast-time until scenario ends or --ff limit reached
    max_sim_time = args.fastforward  # None means "run until scenario STOP"
    next_print_time = 0.0

    print("Starting headless fast-time simulation...")
    try:
        while True:
            # Process any queued stack commands (IC, CRE, WINDGFS, etc.)
            bs.stack.process()

            # Advance one simulation timestep as fast as possible
            bs.sim.step()

            # Tick wall-time dependent helpers (timed_functions, etc.)
            Timer.update_timers()

            # Optional: Print progress every 100 seconds of simulation time
            if bs.sim.simt >= next_print_time:
                print(f"Simulation progress: {bs.sim.simt:.1f} s (Traffic: {bs.traf.ntraf})")
                next_print_time += 100.0

            # --- Termination conditions ---

            # 1. Scenario issued STOP command → state goes to END
            if bs.sim.state == bs.END:
                print("Scenario STOP reached. Simulation complete.")
                break

            # 2. User supplied --ff and we've covered that much sim-time
            if max_sim_time is not None and bs.sim.simt >= max_sim_time:
                print(f"Fast-forward limit reached ({max_sim_time:.0f} s sim-time).")
                break

    except KeyboardInterrupt:
        print("\nInterrupted by user.")

    # --- Save outputs ---
    print(f"Simulation finished at simt={bs.sim.simt:.1f} s  (ntraf={bs.traf.ntraf})")
    scen_stem = os.path.basename(scenario_file)
    bs.stack.stack(f"SAVEATMOS {scen_stem}_atmos")
    bs.stack.stack(f"SAVETRAJ  {scen_stem}_traj")
    bs.stack.stack(f"SAVEHEADER {scen_stem}_header")
    bs.stack.process()

    print("Quitting BlueSky...")
    bs.sim.quit()
    print("Done.")


if __name__ == "__main__":
    main()
