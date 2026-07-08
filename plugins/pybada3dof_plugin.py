"""
pybada3dof_plugin.py

BlueSky plugin entry point. Drop the `pybada3dof/` package and this file
into your BlueSky `plugins/` folder and load it from the stack with:

    PLUGIN LOAD PYBADA3DOF_PLUGIN
    PERFMODEL BADA3        (or BADA4 - BADA4 is the default on load)

This file intentionally contains almost no logic: it wires up
``PyBada3DOFPerf`` (bridge.py) and exposes the ``PERFMODEL`` and
``DYNMODE`` stack commands. Everything else lives in the ``pybada3dof``
package described in the architecture proposal (guidance/, energy/,
control/, dynamics/).

DYNMODE values
--------------
  0  Kinematic autopilot  — BlueSky's native update_airspeed() governs TAS,
                            VS and heading transitions (same as dynamic_bada
                            DYNMODE 0 / pybadaperf).  The BADA performance
                            model still runs for envelope / fuel bookkeeping.
  1  3-DOF physics (default) — the Point-Mass TEM pipeline replaces the
                            kinematic autopilot: forces are integrated via
                            BADA MCMB/LIDL thrust and the energy-share
                            factor governs the ROCD/acceleration trade-off.
"""

import os
import sys

import bluesky as bs

# BlueSky's plugin loader imports this file as a standalone module (not as
# part of a package), so a plain `import pybada3dof` can fail with
# "No module named 'pybada3dof'" depending on the loader's working
# directory / sys.path setup. Making the path explicit here removes that
# dependency on how/from-where BlueSky happens to load plugins.
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from bluesky import stack

from pybada3dof.bridge import PyBada3DOFPerf


def init_plugin():
    PyBada3DOFPerf.select()

    active = PyBada3DOFPerf.instance
    print("=" * 64)
    print("  PYBADA3DOF  -  Point-Mass 3-DOF + TEM guidance plugin")
    print(f"  Active performance model : BADA {active.BADA_VER}")
    print(f"  Data directory           : {active.BADA_DIR}")
    print("  DYNMODE 1 (3-DOF physics) active by default.")
    print("  Use 'DYNMODE 0' to revert to BlueSky kinematic autopilot.")
    print("=" * 64)

    return {
        "plugin_name": "PYBADA3DOF_PLUGIN",
        "plugin_type": "sim",
    }


@stack.command(name="PERFMODEL")
def perfmodel(model: str):
    """PERFMODEL BADA3|BADA4 - switch the active pyBADA performance model.

    Safe to call mid-simulation: existing aircraft are re-created against
    the newly selected model, and the Guidance/Control/Dynamics stack
    keeps running unchanged (it depends on the model through an
    indirection, never on a fixed BADA3/BADA4 reference).
    """
    model = model.upper().strip()
    if model not in ("BADA3", "BADA4"):
        return False, f"Unknown model '{model}'. Use BADA3 or BADA4."

    perf = PyBada3DOFPerf.instance
    if perf is None:
        return False, "PyBada3DOFPerf has not been instantiated yet."

    perf.set_model(model)
    return True, f"Performance model switched to BADA {perf.BADA_VER} ({perf.active_model_name})"


_MODE_NAMES = {
    0: "Kinematic autopilot (BlueSky native)",
    1: "3-DOF BADA physics (TEM pipeline)",
}


@stack.command(name="DYNMODE", annotations="[txt],[txt]")
def dynmode(arg1=None, arg2=None):
    """DYNMODE [acid] <0|1>  — select dynamics fidelity per aircraft or globally.

    DYNMODE 0   Kinematic autopilot: BlueSky's native update_airspeed()
                governs TAS, VS and heading (same as pybadaperf / dynamic_bada
                DYNMODE 0).  BADA still runs for envelope / fuel bookkeeping.
    DYNMODE 1   3-DOF physics (default): forces integrated via BADA
                MCMB/LIDL thrust and the Point-Mass TEM pipeline.

    Examples
    --------
        DYNMODE 0           set ALL aircraft to kinematic mode
        DYNMODE KL001 1     set KL001 to 3-DOF physics
        DYNMODE             show current mode for all aircraft
    """
    perf = PyBada3DOFPerf.instance
    if perf is None:
        return False, "PyBada3DOFPerf has not been instantiated yet."

    # --- no arguments: show status -----------------------------------------
    if arg1 is None:
        lines = ["DYNMODE status:"]
        for i in range(bs.traf.ntraf):
            m = int(perf.dyn_mode[i]) if i < len(perf.dyn_mode) else 1
            lines.append(f"  {bs.traf.id[i]}: {m} — {_MODE_NAMES.get(m, '?')}")
        if bs.traf.ntraf == 0:
            lines.append("  (no aircraft)")
        return True, "\n".join(lines)

    # --- parse arguments ----------------------------------------------------
    if arg2 is None:
        # DYNMODE <mode>  — global
        try:
            mode = int(arg1)
        except ValueError:
            return False, "DYNMODE: mode must be 0 or 1"
        idx = None
    else:
        # DYNMODE <acid> <mode>  — per aircraft
        acid_str = arg1.upper()
        idx = bs.traf.id2idx(acid_str)
        if idx < 0:
            return False, f"Aircraft '{acid_str}' not found"
        try:
            mode = int(arg2)
        except ValueError:
            return False, "DYNMODE: mode must be 0 or 1"

    if mode not in (0, 1):
        return False, "DYNMODE: mode must be 0 or 1"

    # --- apply ---------------------------------------------------------------
    if idx is None:
        perf.dyn_mode[:] = mode
        msg = f"All aircraft: DYNMODE {mode} — {_MODE_NAMES[mode]}"
    else:
        perf.dyn_mode[idx] = mode
        msg = f"{bs.traf.id[idx]}: DYNMODE {mode} — {_MODE_NAMES[mode]}"

    print(f"[pybada3dof] {msg}")
    return True, msg


@stack.command(name="MASS", annotations="txt,float")
def mass(acid: str, mass_kg: float):
    """MASS <acid> <mass_kg>  — override aircraft mass [kg] immediately.

    Sets the mass used by the 3-DOF TEM pipeline for the named aircraft.
    Safe to call mid-simulation: takes effect at the very next timestep.

    Example
    -------
        MASS KL001 65000
    """
    perf = PyBada3DOFPerf.instance
    if perf is None:
        return False, "PyBada3DOFPerf has not been instantiated yet."

    idx = bs.traf.id2idx(acid.upper())
    if idx < 0:
        return False, f"Aircraft '{acid.upper()}' not found."

    if mass_kg <= 0:
        return False, f"MASS: mass must be positive (got {mass_kg} kg)."

    perf.mass[idx] = mass_kg
    msg = f"{bs.traf.id[idx]}: mass set to {mass_kg:.1f} kg"
    print(f"[pybada3dof] {msg}")
    return True, msg


@stack.command(name="SPDSCHED", annotations="txt")
def spdsched(schedule: str = ""):
    """SPDSCHED [ICAO|CONSCAS]  — select the climb/descent speed schedule.

    ICAO (default)
        Standard ICAO/BADA speed schedule: constant Mach above the crossover
        altitude (~FL300 for BADA4 DUMMY), constant CAS below.  This is the
        physically accurate BADA TEM schedule.

    CONSCAS
        Constant CAS throughout the entire flight — no Mach phase.
        Useful for comparison runs, low-altitude studies, or to match a
        scenario that commands a single CAS from departure to destination.

    SPDSCHED (no argument)
        Show the currently active schedule.

    Example
    -------
        SPDSCHED CONSCAS   — switch to constant-CAS everywhere
        SPDSCHED ICAO      — revert to the default ICAO schedule
    """
    import plugins.pybada3dof.guidance.reference_generator as _rg

    if not schedule:
        return True, f"Active speed schedule: {_rg.SPEED_SCHEDULE}"

    s = schedule.upper().strip()
    if s not in ("ICAO", "CONSCAS"):
        return False, f"SPDSCHED: unknown schedule '{schedule}'. Use ICAO or CONSCAS."

    _rg.SPEED_SCHEDULE = s
    msg = f"Speed schedule set to {s}"
    print(f"[pybada3dof] {msg}")
    return True, msg
