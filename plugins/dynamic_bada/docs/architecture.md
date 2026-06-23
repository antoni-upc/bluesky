# dynamic_bada — Architecture Overview

## Package layout

```
plugins/dynamic_bada/
├── __init__.py            Package marker + re-exports
├── config.yaml            Master configuration file
├── config.py              Typed config dataclass (DynBadaConfig)
├── bada_interface.py      Unified BADA3/BADA4 performance adapter & atmosphere state
├── flight_dynamics.py     Pure-function flight physics (point-mass force balance)
├── dynamic_aircraft.py    Per-aircraft state, phase machine & integration step
├── plugin.py              BlueSky PerfBase subclass (entry point)
├── validation/            Standalone unit tests (no BlueSky needed)
└── examples/              BlueSky scenario files
```

## Data flow (one simulation tick)

```
BlueSky Traffic.update()
  ├── Autopilot.update()            ← LNAV / VNAV logic (unchanged)
  ├── DynamicBada.update(dt)        ← our plugin (timed_function, preupdate)
  │     for each aircraft i:
  │       read alt, tas, vs, hdg, mass, ax from bs.traf
  │       compute atmosphere via bada_interface.py → pyBADA.atmosphere
  │       build GuidanceCommand from bs.traf.ap / bs.traf.aporasas
  │       call DynamicAircraft.step()
  │         ├── BadaInterface.forces()   → pyBADA Thrust / Drag / Lift
  │         ├── BadaInterface.rocd()     → pyBADA ROCD (energy equation)
  │         └── BadaInterface.fuelflow() → pyBADA fuel flow
  │       write results back:
  │         bs.traf.vs[i], bs.traf.ax[i]
  │         PerfBase: mass, thrust, drag, fuelflow
  ├── aporasas.update()
  ├── perf.limits()                 ← envelope enforcement from pyBADA
  ├── update_airspeed()             ← uses our pre-written vs/ax
  ├── update_groundspeed()
  └── update_pos()
```

## Module responsibilities

| Module | Role | External deps |
|--------|------|---------------|
| `config.py` | Load YAML, typed singleton | PyYAML |
| `bada_interface.py` | BADA3/4 adapter, model cache, synonym resolution, and ISA atmosphere wrapper | pyBADA.bada3, pyBADA.bada4, pyBADA.atmosphere |
| `flight_dynamics.py` | Pure-function point-mass physics: force balance, flight-path angle, angle wrapping | **none** |
| `dynamic_aircraft.py` | State propagation, phase state machine, and fidelity dispatch | all above |
| `plugin.py` | BlueSky PerfBase, write-back, stack commands | BlueSky, all above |

## Fidelity modes

| Mode | Heading | VS | ax |
|------|---------|----|----|
| 0 | BlueSky | BlueSky | BlueSky |
| 1 | BlueSky | pyBADA ROCD | force balance |
