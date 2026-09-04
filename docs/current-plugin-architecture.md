# Current research-plugin architecture

## Purpose and status

This is the current-code map for PYBADATEM, ERA5, GFS, and
RESEARCHRECORDER. It distinguishes original BlueSky responsibilities, the
minimal coexisting hooks added to BlueSky, and plugin-owned behavior. Current
validation status and claim boundaries are maintained in
[`reproducibility-matrix.md`](reproducibility-matrix.md).

The dependency-free, licensed BADA, weather/TEM, schema-compatibility, and
plugin-disabled gates are closed for the scope recorded in the matrix. Tests
requiring licensed BADA data or external weather resources remain separately
marked and require a validated local run manifest.

## Component map

```mermaid
flowchart LR
    subgraph BS[Original BlueSky responsibilities]
        CMD[Stack commands and scenarios]
        AP[Autopilot + LNAV/VNAV]
        AR[AP/ASAS resolved targets]
        TRAF[Traffic propagation]
        POS[Heading, position, trails]
    end
    subgraph HOOKS[Minimal BlueSky integration hooks]
        PERFHOOK[PerfBase.update_dynamics\nspeed/vertical ownership masks]
        ATMHOOK[WindSim.get_atmosphere\noptional atmosphere sample]
        STATE[Applied-atmosphere\nand provenance arrays]
    end
    subgraph PLUGINS[Research plugins]
        PBT[PYBADATEM entry point]
        PB[BADA 3/4 adapter + TEM\nenvelopes and quality events]
        WX[ERA5 / GFS thin providers]
        CUBE[Shared weather cube\ntime policy and interpolation]
        REC[RESEARCHRECORDER\nstreaming CSV + metadata/events]
    end
    CMD --> AP --> AR --> TRAF --> POS
    PBT --> PB --> PERFHOOK --> TRAF
    WX --> CUBE --> ATMHOOK --> STATE --> TRAF
    TRAF -. sampled state .-> REC
    PB -. quality events .-> REC
```

The hooks are inert when the plugins are not selected. The closed disabled
plugin baseline produced byte-identical final-state JSON against the tested
upstream revision.

## Per-tick logic today

```mermaid
sequenceDiagram
    participant AP as BlueSky autopilot/LNAV/VNAV
    participant T as Traffic.update
    participant P as Selected performance model
    participant B as PYBADATEM adapter
    participant W as Active wind/weather provider
    participant R as Research recorder
    AP->>T: resolved TAS, VS, altitude, heading targets
    T->>P: limits(targets, previous acceleration)
    T->>P: update_dynamics(current state, dt)
    opt PYBADATEM selected
        P->>B: current atmosphere, TAS, mass, phase, schedule
        B-->>P: thrust, rated thrust, drag, fuel, ESF, ROCD
        P-->>T: native speed / optional TEM vertical ownership
    end
    T->>T: native selected-speed step and target capture
    T->>T: heading, vertical capture when native, position
    T->>W: atmosphere at new position and simulation UTC
    W-->>T: ERA5/GFS sample or no provider sample
    T->>T: apply provider state or explicit ISA fallback
    R->>T: sample applied traffic/performance/provenance state
```

### Horizontal-energy implementation status

Historically the speed request was calculated after `update_dynamics`, so
PYBADATEM could report level-flight `thrust = drag` while BlueSky changed TAS.
The current implementation now calculates a typed `SpeedStepRequest` before
the performance hook and uses it for both PYBADATEM evaluation and native
propagation.

For level flight, BADA 3 uses public `TAdapted`; BADA 4 uses the equivalent
required-thrust equation and CT-based fuel evaluation. Required, idle, and
maximum thrust plus requested/applied acceleration and limitation state are
retained per aircraft. Strict mode rejects a request outside the thrust bounds
by holding the simulation without terminating BlueSky.
The adapter now implements thrust-feasible horizontal saturation and one joint
horizontal/vertical energy allocation in TEM mode:

```mermaid
flowchart LR
    TARGET[Resolved target TAS] --> REQUEST[Native speed-step request\ntarget, requested ax, capture, next TAS]
    REQUEST --> BADA[BADA adapted-thrust evaluation]
    BADA --> LIMIT[Thrust bounds + applied energy allocation]
    LIMIT --> MODE{Dynamics mode}
    MODE -->|KINEMATIC| NATIVE[BlueSky applies native step\nBADA records required force/fuel]
    MODE -->|TEM| TEM[BADA applies feasible horizontal/vertical result]
    NATIVE --> EVIDENCE[Recorder force-balance provenance]
    TEM --> EVIDENCE
```

## Plugin state and ownership

| Area | Original BlueSky owns | Plugin owns | Current state |
| --- | --- | --- | --- |
| Navigation | SPD, LNAV/VNAV, waypoint and turn targets | Nothing | Preserved |
| Horizontal propagation | Native target selection and capture | Adapted thrust/fuel and feasibility | Saturation and joint horizontal/vertical allocation implemented and scoped by licensed gates |
| Vertical propagation | Native VS/altitude capture | PYBADATEM owns VS in TEM mode | Implemented and envelope-checked |
| Performance | Replaceable performance selection | BADA 3/4 resolution, force/fuel/mass, strict failures | Implemented; steady cruise defensible |
| Envelopes | No research policy | Per-aircraft OFF/REPORT/ENFORCE/ABORT | BADA 3.15 and 4.2 scoped validation closed |
| Atmosphere | ISA initialization and airdata | ERA5/GFS temperature, pressure, density, wind and provenance | Implemented and validated |
| Weather time | Simulation UTC | Exact provider slots and opt-in interpolation | ERA5 hourly; GFS six-hourly |
| Invalid weather | ISA remains available | Strict abort or explicit interactive ISA fallback | Implemented; no extrapolation |
| Evidence | Simulation state | Versioned streaming CSV, metadata, quality events | `samples-v10`, bounded memory |

## Lifecycle map

```mermaid
stateDiagram-v2
    [*] --> Unloaded
    Unloaded --> Loaded: PLUGIN LOAD
    Loaded --> Configured: paths/version/policy selected
    Configured --> Active: model family or weather cube accepted
    Active --> Running: aircraft update
    Running --> Running: valid tick + evidence
    Running --> Fallback: interactive weather failure
    Fallback --> Running: valid cube loaded
    Running --> Held: strict failure or ABORT event
    Active --> Cleared: provider clear/reset
    Running --> Cleared: simulation reset
    Cleared --> Configured
```

PYBADATEM resolves aircraft before Traffic arrays are resized, maintains
per-aircraft model/envelope state through create/delete/reset, and switches BADA
families transactionally. Meteorology accepts a cube only after schema and
content validation; failed or expired time slots never retain stale weather.
The recorder streams rows and closes authoritative CSV, metadata, and event
evidence on stop/reset or an ABORT event.

## Current claim boundary

Safe claims include exact tested weather-slot behavior, bounded spatial and
vertical interpolation, explicit fallback provenance, BADA resolution and
envelope behavior, route/speed target capture, and constant-speed level-flight
`thrust = drag`.

Level-flight adapted-thrust force balance and joint horizontal/vertical energy
allocation are covered by packaged and licensed BADA 3/4 gates in their
recorded scope. The clean operational matrix adds one BADA 4.2 A320-232 route
under enforced mass, CAS, altitude, ROC, and ROD checks. It is not evidence for
non-clean terminal configurations, phase-aware Mach limits, or the preserved
FL390 operational trajectory; those boundaries are recorded in
`research-modeling-open-issues.md`. Route geometry remains BlueSky guidance
evidence.

## Branch architecture

| Branch | Ownership |
| --- | --- |
| `plugin/recorder` | Streaming recorder and quality-event observation |
| `plugin/NWP-meteo` | ERA5/GFS providers, weather cubes, cache tools, and atmosphere hook |
| `plugin/pybada-tem` | PyBADA adapter, TEM dynamics, envelopes, and performance hooks |
| `integration/plugin-stack` | Reviewed composition of all three plugins |
| `research/reproducibility` | Scenarios, manifests, matrix runner, validators, and CI |

The matrix runner and validators are analysis code, not a fourth production
plugin. Shared host hooks remain inert when their plugin is not selected. The
composition is checked against the pinned plugin-disabled OpenAP/ISA baseline.

## Related documents

- `research-plugins.md`: operator-facing setup, commands, and validation.
- `bada-envelope-implementation.md`: envelope behavior and licensed scope.
- `reproducibility-matrix.md`: scenarios, comparison semantics, and validation.
- `research-modeling-open-issues.md`: deliberately unresolved questions.
