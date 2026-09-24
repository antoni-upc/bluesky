# Per-aircraft BADA envelope implementation

## Implementation scope

PYBADATEM now owns an optional, per-aircraft BADA envelope layer independently
of its propagation mode. `DYNAMICS acid KINEMATIC|TEM` selects propagation;
`ENVELOPE` and `ENVELOPECHECKS` select feasibility behaviour. A KINEMATIC
aircraft can therefore be checked and a TEM aircraft can remain unchecked.

In both modes BlueSky retains `SPD`, LNAV/VNAV waypoint constraints, and the
selected-speed target. In `KINEMATIC`, BlueSky applies its native acceleration
and vertical response. In `TEM`, PYBADATEM applies the thrust-feasible
speed-priority acceleration and geometric vertical rate, including exact target
capture when feasible, and evaluates thrust, drag, fuel flow, and aerodynamic
configuration. Scenario routes therefore preserve BlueSky guidance intent
without allowing native propagation to bypass the TEM energy balance.

### Selected-speed representation limit

BlueSky parses a CAS command into m/s and a Mach command into a dimensionless
number. `SPD` retains that selected representation. `ADDWPT` retains it in the
route and passes it through later waypoint transitions. BlueSky's built-in
`DIRECT` operation also runs when an added waypoint first activates a route;
its usual behavior converts that initial Mach waypoint target to CAS.
PYBADATEM requests raw Mach retention for that `DIRECT` path so the first leg
can use `constM`. Native performance models keep BlueSky's conversion.

`CRE` and `MOVE` initialize the selected speed from the computed CAS even when
their input is Mach. They set the aircraft's physical Mach state, but do not
establish a Mach selected-speed law. Use `SPD M...` or a Mach waypoint with
active VNAV speed guidance when a `constM` target is intended. The recorder
captures the law actually evaluated by PYBADATEM; it cannot recover a lost
source representation from a CAS value. Existing all-CAS campaign inputs
therefore continue to evaluate as `constCAS` until those inputs are changed.

The implemented checks are:

- mass: `MASS_MIN`, `MASS_MAX`;
- speed and altitude: `LOW_SPEED`, `HIGH_SPEED`, `MACH_MIN`, `MACH_MAX`,
  `ALTITUDE_MAX`;
- vertical performance: `ROC_MAX`, `ROD_MAX`;
- coordinated-turn lateral limits: `BANK_ANGLE`, `LOAD_FACTOR`.

Dependency-free tests cover BADA 3 and BADA 4 adapter interfaces, policy
behaviour, transactional mutation, event transitions, aircraft isolation,
recorder persistence, and create/delete/reset/family-switch state alignment.
Scenario and validator pairs are provided for licensed BADA 3 and BADA 4 data,
but they become evidence only when rerun against the exact commit and resources
declared for an experiment.

### Configuration selection

The BADA 4 adapter asks pyBADA for the active aerodynamic configuration and
contains selection logic for both DLM limit groups:

- `HLid == 0`: clean `n3..n1`;
- `HLid > 0`: high-lift `nf3..nf1`.

The supplied BADA 4 scenario set exercises `CR`, `IC`, `AP`, `TO`, and `LD`
selection, including gear-up and gear-down paths. The BADA 3 set exercises
aircraft resolution, observation, mass, CAS/Mach/altitude, direct MOVE/CRE
transactions, vertical rate, bank/load, lifecycle isolation, and route
integration. These are validation interfaces, not transferable results: each
aircraft and dataset version requires its own retained evidence.

## Commands and defaults

```text
ENVELOPE [acid] [OFF|REPORT|ENFORCE|ABORT]
ENVELOPECHECKS acid [CORE_ONLY|LONGITUDINAL|FULL|CUSTOM <checks>]
BADACONFIG acid [CRUISE|PYBADA]
PERFSTATUS [acid] [CURRENT|BOUNDS|ALL]
```

New aircraft default to:

- policy `OFF`;
- profile `LONGITUDINAL`;
- no explicit custom list, so the profile expands to its defined checks;
- BADA configuration mode `PYBADA`, preserving existing adaptive behaviour.

`BADACONFIG` controls one aircraft's BADA aerodynamic-configuration source.
`CRUISE` forces configuration `CR` consistently for TEM energy calculations
and envelope calculations. `PYBADA` supplies BlueSky's climb/cruise/descent
intent plus the operating state to `pyBADA.getConfig()`. The resulting
configuration is mapped through `getAeroConfig()` to `HLid` and landing gear.
`HLid` is an attribute of the selected configuration, not a landing detector.
A future plugin-owned `MANAGED` state machine remains a design option but is
not currently accepted by the command.

`LONGITUDINAL` contains mass, speed, Mach, altitude, ROC, and ROD checks. It
excludes `BANK_ANGLE` and `LOAD_FACTOR`. `FULL` includes all checks. `CUSTOM`
uses exactly the supplied check identifiers. `CORE_ONLY` selects no optional
BADA checks, but fundamental validation remains active.

`OFF` disables selected BADA feasibility checks. It never disables fundamental
validation such as finite positive mass, finite model output, positive pressure
and temperature, or consistent bounds.

Commands without a new value report the addressed aircraft's current setting.
Existing aircraft retain their individual configuration when defaults change.
`PERFSTATUS` defaults to `ALL` and separates current state, evaluated bounds,
and quality state into labelled sections. `CURRENT` and `BOUNDS` provide compact
views. `MAX` and `MAXS` are accepted as compatibility aliases for `BOUNDS`,
since the view also contains minimum values. Altitudes retain SI metres and add
feet/flight level in parentheses.

## Policy behaviour

### REPORT

An infeasible request or runtime state is accepted, the addressed aircraft is
marked `INFEASIBLE`, and one quality event is emitted when a reason begins or
changes. Recovery permits a later recurrence event. No other aircraft is
mutated.

### ENFORCE

Direct state assignments are transactional: an infeasible `MASS`, `MOVE`,
creation, or configuration transition is rejected and prior state is
preserved. Resolved guidance is atomically limited to the selected bounds.
Requested and applied values are included in the quality event.

For lateral enforcement, PYBADATEM limits the existing per-aircraft BlueSky
bank setting. It does not replace BlueSky lateral guidance or introduce a roll
dynamics model.

### ABORT

The responsible aircraft and violation are published, the optional recorder
synchronously writes its event, final sample, and metadata, and the simulation
enters `HOLD`. The event uses continuation `STOP`.

## Bound sources and conventions

### Mass

OEW and MTOW are normalised for BADA 3 and BADA 4. Missing, non-finite, or
contradictory values produce `UNKNOWN`. Positive finite mass remains a
fundamental requirement under every policy. Fuel burn crossing an enabled
minimum is evaluated as a runtime-derived violation.

### CAS, Mach, and altitude

Bounds are evaluated per aircraft at its current operating point using the
atmosphere already applied to that aircraft. Guidance enforcement records
requested and applied speed and altitude. Direct `MOVE` and creation checks are
transactional.

`ALTITUDE_MAX` is a pressure-altitude bound. Current-state checks compare it
with `traf.pressure_alt`. Guidance altitude targets remain geometric; the
plugin samples pressure at the target altitude to check them and finds a
geometric target at the ceiling when enforcement is needed. Recorder event
`altitude_m` values remain geometric requests and applied targets, while
`maximum_altitude_m` records the pressure-altitude ceiling.

### ROC and ROD

The dynamic climb maximum is obtained from MCMB and the descent maximum from
LIDL TEM evaluations at the same operating point. BlueSky VS commands use feet
per minute. PYBADATEM converts to SI and evaluates signed vertical rate
internally, while user-facing quality output reports direction plus positive
magnitude. `PERFSTATUS` reports positive `ROC_MAX` and `ROD_MAX` magnitudes.

No native BlueSky VS convention was changed.

### Bank angle and load factor

BADA 4 DLM `n1`, `n3`, `nf1`, and `nf3` are read from the licensed aircraft
XML because the current pyBADA object does not expose them. The parsed values
are cached per model. For BADA 3, the adapter uses the phase-dependent civilian
maximum bank angle exposed by pyBADA and derives its positive load ceiling.
It does not invent a BADA 3 negative load-factor limit.

The evaluated load factor is the coordinated level-turn value:

```text
nz = 1 / cos(abs(bank angle))
```

BlueSky does not model a separate roll transient here. `current_bank` and the
recorded `bank_angle_deg` are the effective-commanded turn bank: zero outside
a selected turn and the selected bank during a turn. The lateral gate therefore
checks coordinated-turn guidance, not measured six-degree-of-freedom normal
acceleration.

Negative DLM load limits are recorded when available but cannot be exercised by
the positive `1/cos(bank)` coordinated-level-turn model. Numerical limits are
dataset and configuration inputs and must be read from each run's evidence.

## Quality events and recording

Every new event is printed to both the process terminal and the interactive
BlueSky console. Fields include aircraft, component, reason, policy, action,
requested values, applied values, and continuation.

The recorder is optional and does not influence envelope or simulation
behaviour. When active it writes:

- `run.csv` using the exact active schema `samples-v12`;
- synchronously flushed `run.events.jsonl`;
- `run.metadata.json` with effective policy/checks, event and reason totals,
  and sticky `VALID`, `DEGRADED`, or `ABORTED` quality status.

The CSV includes current policy, expanded checks, feasibility, last action and
reason, counters, all longitudinal/vertical bounds, lateral configuration,
effective bank, coordinated load factor, and lateral bounds.

On the integrated stack, the recorder-owned contract defines the exact CSV,
event, and metadata interface.

Persistent violations emit events on transitions only. Scheduled CSV samples
continue to expose current status on every row.

## Interactive gates

Run these in the BlueSky console:

```text
IC research/pybada-envelope-mass
IC research/pybada-envelope-abort
IC research/pybada-envelope-flight
IC research/pybada-envelope-direct
IC research/pybada-envelope-flight-abort
IC research/pybada-envelope-vertical
IC research/pybada-envelope-vertical-direct
IC research/pybada-envelope-vertical-abort
IC research/pybada-envelope-lateral
IC research/pybada-envelope-lateral-abort
IC research/pybada-route-speed-gui
IC research/pybada-envelope-highlift
IC research/pybada-envelope-highlift-abort
IC research/pybada-envelope-approach
IC research/pybada-envelope-approach-abort
IC research/pybada-envelope-terminal-observe
IC research/pybada-envelope-terminal
IC research/pybada-envelope-takeoff-abort
IC research/pybada-envelope-landing-abort
```

The route-speed GUI gate uses four A320s on separate roughly 20-by-20-km
squares with BlueSky's default `FLYBY` turns. It crosses envelope
`REPORT`/`OFF` with LNAV+VNAV `ON`/`OFF`, using
red trails for REPORT and blue trails for OFF. The navigation-on aircraft must
follow their routes and waypoint constraints; navigation-off aircraft must
continue straight. REPORT may add quality evidence but must not change the
flown path.

The high-lift gate requires the intended initial-climb configuration, matching
high-lift DLM bounds, REPORT/ENFORCE separation, ABORT finalisation, policy
isolation, and final effective-configuration metadata. Its validator is
`tests/research/validate_highlift_lateral_run.py`.

The approach and terminal gates similarly require their intended configuration
and gear state, matching lateral bounds, transition-only events, policy
isolation, and metadata agreement. Their numerical limits are read from the
licensed model and checked by the scenario-specific validators rather than
asserted here as durable documentation.

The scenarios prefix explanations with `TEST INFO`, actions with `TEST ACTION`,
and expected outcomes with `TEST EXPECT`. The lateral scenario enables red and
blue GUI trails: the red 75-degree REPORT path is expected to turn more tightly
than the blue BADA-limited ENFORCE path.

Validate the generated evidence with:

```shell
python3 tests/research/validate_envelope_run.py output/pybada-envelope-mass.csv
python3 tests/research/validate_envelope_run.py output/pybada-envelope-abort.csv --abort
python3 tests/research/validate_flight_envelope_run.py output/pybada-envelope-flight.csv
python3 tests/research/validate_flight_envelope_run.py output/pybada-envelope-direct.csv --direct
python3 tests/research/validate_flight_envelope_run.py output/pybada-envelope-flight-abort.csv --abort
python3 tests/research/validate_vertical_envelope_run.py output/pybada-envelope-vertical.csv
python3 tests/research/validate_vertical_envelope_run.py output/pybada-envelope-vertical-direct.csv --direct
python3 tests/research/validate_vertical_envelope_run.py output/pybada-envelope-vertical-abort.csv --abort
python3 tests/research/validate_lateral_envelope_run.py output/pybada-envelope-lateral.csv
python3 tests/research/validate_lateral_envelope_run.py output/pybada-envelope-lateral-abort.csv --abort
python3 tests/research/validate_highlift_lateral_run.py output/pybada-envelope-highlift.csv
python3 tests/research/validate_highlift_lateral_run.py output/pybada-envelope-highlift-abort.csv --abort
python3 tests/research/validate_approach_lateral_run.py output/pybada-envelope-approach.csv
python3 tests/research/validate_approach_lateral_run.py output/pybada-envelope-approach-abort.csv --abort
python3 tests/research/validate_terminal_observation.py output/pybada-envelope-terminal-observe.csv
python3 tests/research/validate_terminal_lateral_run.py output/pybada-envelope-terminal.csv
python3 tests/research/validate_terminal_lateral_run.py output/pybada-envelope-takeoff-abort.csv --abort TO
python3 tests/research/validate_terminal_lateral_run.py output/pybada-envelope-landing-abort.csv --abort LD
python3 tests/research/validate_route_comparison.py output/pybada-route-speed-gui.csv
```

Validators report `INVALID evidence` with individual reasons and exit nonzero;
they do not expose raw assertion tracebacks for ordinary invalid evidence.

## Evidence policy

Run `tests/research/run_pybada_revalidation.py` to execute the complete licensed
scenario/validator catalogue, including horizontal acceleration, thrust
saturation, joint horizontal/vertical energy, turn load and energy, envelope,
route, lifecycle, and timestep cases for both families. `--validate-only` is
appropriate only for artefacts generated by the same declared commit and
resource set.

A passing dependency-free suite establishes adapter and policy logic with test
doubles. A passing licensed scenario establishes only its declared aircraft,
dataset version, configuration, state, timestep, and validator. Neither closes
untested aircraft, datasets, configurations, routes, or operating regimes.
Current evidence status belongs in the retained run manifest and validator
output defined by the integration validation protocol.

Generic non-clean configuration management, phase-aware speed limits, and
other unresolved modelling questions are tracked by the integration stack.
