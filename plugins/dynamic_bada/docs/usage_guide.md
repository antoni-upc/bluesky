# dynamic_bada — Usage Guide

## Loading the plugin

```
; In a scenario file:
PLUGINS LOAD dynamic_bada

; Or at the BlueSky console:
PLUGIN LOAD dynamic_bada
```

The plugin automatically reads `plugins/dynamic_bada/config.yaml` and
prints a banner showing the active configuration.

## Stack commands

### DYNMODE — set fidelity

```
DYNMODE 1            ; all aircraft → point-mass dynamic (default)
DYNMODE ACI001 0     ; ACI001 only  → legacy kinematic
DYNMODE              ; show help text
```

| Mode | Description |
|------|-------------|
| 0 | Legacy BlueSky kinematic — no overrides |
| 1 | Point-mass: pyBADA ROCD + force-balance ax [DEFAULT] |

### DYNBADA — switch BADA generation

```
DYNBADA 4            ; all aircraft use BADA 4 (default)
DYNBADA ACI001 3     ; ACI001 uses BADA 3
```

### DYNSTATS — inspect one aircraft

```
DYNSTATS ACI001
```

Prints: mode, BADA version, mass, thrust, drag, fuel flow, VS, ax,
vmin, vmax, vstall, hmax.

### DYNRESET — reload config without restart

```
DYNRESET
```

Re-reads `config.yaml`.  Useful when tuning parameters during a session.

## Configuration (config.yaml)

Key fields:

```yaml
default_bada_version: 3        # 3 or 4
default_fidelity_mode: 1       # 0 or 1
bada4_dir: "..."               # path to BADA4 data
bada3_dir: "..."               # path to BADA3 data
vs_threshold_climb_m_s:   0.5
vs_threshold_descent_m_s: -0.5
min_tas_m_s: 1.0
```

## Aircraft model selection

Models are loaded from the pyBADA dummy databases automatically:

**BADA 4 (preferred)**:
- Exact match: folder with that name in `bada4_dir`
- Heuristic: `C*` / `PA*` / `BE*` → `Dummy-PST` (piston); `AT*` → `Dummy-TBP` (turboprop)
- Default fallback: `Dummy-TWIN` (twin-jet)

**BADA 3**:
- Exact match: type name in the DUMMY data files
- Synonym table: `SYNONYM.NEW` maps ICAO codes (A320, B738, …) to generic file stems
- Default fallback: `J2H___` (large twin-jet)

## Example scenarios

| File | Tests |
|------|-------|
| `examples/02_climb_capture.scn` | BADA ROCD, fuel burn in MODE 1 |

## Running validation tests

```bash
# From the BlueSky root:
python plugins/dynamic_bada/validation/test_flight_dynamics.py
python plugins/dynamic_bada/validation/test_bada_interface.py
```

Or with pytest:
```bash
pytest plugins/dynamic_bada/validation/
```
