# Per-aircraft BADA envelope implementation

For the exact resume point, accepted evidence, next-session order, and working
tree cautions, see
[`bada-envelope-next-session.md`](bada-envelope-next-session.md).

## Status and validated scope

PYBADATEM now owns an optional, per-aircraft BADA envelope layer independently
from its propagation mode. `DYNAMICS acid KINEMATIC|TEM` selects propagation;
`ENVELOPE` and `ENVELOPECHECKS` select feasibility behavior. A KINEMATIC
aircraft can therefore be checked and a TEM aircraft can remain unchecked.

In both modes BlueSky retains horizontal-speed ownership, including `SPD`,
LNAV/VNAV waypoint constraints, selected-speed capture, and native acceleration
limits. `TEM` owns vertical rate from pyBADA energy evaluation and continues to
evaluate thrust, drag, fuel flow, and aerodynamic configuration. This split is
intentional so scenario routes preserve BlueSky guidance semantics.

The implemented checks are:

- mass: `MASS_MIN`, `MASS_MAX`;
- speed and altitude: `LOW_SPEED`, `HIGH_SPEED`, `MACH_MIN`, `MACH_MAX`,
  `ALTITUDE_MAX`;
- vertical performance: `ROC_MAX`, `ROD_MAX`;
- coordinated-turn lateral limits: `BANK_ANGLE`, `LOAD_FACTOR`.

Synthetic tests cover BADA 3 and BADA 4 adapters, policy behavior, transactional
mutation, event transitions, aircraft isolation, recorder persistence, and
create/delete/reset/family-switch state alignment. Licensed interactive gates
have been completed with BADA 4.2 `A320-232` for mass, speed/Mach/altitude,
vertical rate, clean-configuration bank/load behavior, and initial-climb
high-lift bank/load behavior.

### Important configuration assumption

Licensed lateral enforcement currently supports the demonstrated `CR` and `IC`
operating points for BADA 4.2 `A320-232`.

The BADA 4 adapter asks pyBADA for the active aerodynamic configuration and
contains selection logic for both DLM limit groups:

- `HLid == 0`: clean `n3..n1`;
- `HLid > 0`: high-lift `nf3..nf1`.

The `CR`, `IC`, `AP`, `TO`, and `LD` paths have passed licensed observation,
REPORT, ENFORCE, and ABORT gates. This includes gear-up and gear-down operation.
For BADA 4.2 `A320-232`, licensed gates cover clean, initial-climb, approach,
take-off, and landing configurations. For BADA 3.15, `A320` resolves
deterministically to `A320__`; licensed gates cover observation, mass,
CAS/Mach/altitude, direct MOVE/CRE transactions, climb/descent rate,
bank/load, lifecycle isolation, and route integration. These results must not
be generalized to other aircraft or dataset versions without their own evidence.

The synthetic adapter test verifies the intended clean/high-lift DLM mapping.
End-to-end licensed CR, IC, AP, TO, and LD runs now provide the corresponding
operational evidence.

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
- BADA configuration mode `PYBADA`, preserving existing adaptive behavior.

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
and quality state into labeled sections. `CURRENT` and `BOUNDS` provide compact
views. `MAXS` is accepted as a compatibility alias for `BOUNDS`, since the view
also contains minimum values. Altitudes retain SI metres and add feet/flight
level in parentheses.

## Policy behavior

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

OEW and MTOW are normalized for BADA 3 and BADA 4. Missing, non-finite, or
contradictory values produce `UNKNOWN`. Positive finite mass remains a
fundamental requirement under every policy. Fuel burn crossing an enabled
minimum is evaluated as a runtime-derived violation.

### CAS, Mach, and altitude

Bounds are evaluated per aircraft at its current operating point using the
atmosphere already applied to that aircraft. Guidance enforcement records
requested and applied speed and altitude. Direct `MOVE` and creation checks are
transactional.

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

The observed load factor is the coordinated level-turn value:

```text
nz = 1 / cos(abs(bank angle))
```

BlueSky does not model a separate roll transient here. `current_bank` and the
recorded `bank_angle_deg` are the effective commanded turn bank: zero outside
a selected turn and the selected bank during a turn. The lateral gate therefore
checks coordinated-turn guidance, not measured six-degree-of-freedom normal
acceleration.

For licensed BADA 4.2 `A320-232` in `CR`, the observed clean limits are
`n3=-1.0`, `n1=2.5`, corresponding to a maximum coordinated bank of about
66.42 degrees. The negative DLM limit is recorded but cannot be exercised by
the positive `1/cos(bank)` coordinated-level-turn model.

## Quality events and recording

Every new event is printed to both the process terminal and the interactive
BlueSky console. Fields include aircraft, component, reason, policy, action,
requested values, applied values, and continuation.

The recorder is optional and does not influence envelope or simulation
behavior. When active it writes:

- `run.csv` using schema `samples-v7`;
- synchronously flushed `run.events.jsonl`;
- `run.metadata.json` with effective policy/checks, event and reason totals,
  and sticky `VALID`, `DEGRADED`, or `ABORTED` quality status.

The CSV includes current policy, expanded checks, feasibility, last action and
reason, counters, all longitudinal/vertical bounds, lateral configuration,
effective bank, coordinated load factor, and lateral bounds.

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

The high-lift gate requires both
aircraft to remain in observed initial-climb configuration `IC`, `HLid=2`, gear
up, with `nf3/nf1` load bounds `0.0..2.0`. REPORT requests 70 degrees while
ENFORCE must limit to approximately 60 degrees. Its validator is
`tests/research/validate_highlift_lateral_run.py`. Licensed REPORT, ENFORCE,
and ABORT gates have passed. AP, TO, and LD have subsequently passed their
dedicated licensed gates as recorded in the BADA 4 support matrix.

The approach gate uses the previously observed 5,000-ft, 180-kt operating
point and commands a descent toward 1,000 ft at 1,000 ft/min. Its validator requires `AP`, `HLid=3`,
gear up, `nf3/nf1=0.0..2.0`, a bank maximum near 60 degrees, one transition
per aircraft, policy isolation, and final effective-configuration metadata.
Its licensed REPORT/ENFORCE and ABORT validators have passed.

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

## Completed evidence

Licensed BADA 4.2 interactive results accepted during implementation include:

- mass REPORT/ENFORCE isolation and ABORT flush;
- CAS/Mach/altitude REPORT retention, ENFORCE limiting, direct transactional
  state assignment, and ABORT flush;
- dynamic climb/descent REPORT and ENFORCE behavior, direct VS transactions,
  directional magnitude event output, and ABORT flush;
- clean-configuration bank/load REPORT at 75 degrees and load 3.864, ENFORCE
  at 66.42 degrees and load 2.500, transition-only events, per-aircraft
  isolation, GUI turn-radius trails, and ABORT flush.
- initial-climb `IC`, `HLid=2`, gear-up high-lift REPORT at 70 degrees/load
  2.924, ENFORCE at 60 degrees/load 2.000, and synchronous ABORT flush using
  `nf3/nf1=0.0..2.0`.

The current dependency-free research suite passes 93 tests with six
licensed/external deselections and four known NumPy fixture deprecation
warnings. `git diff --check` passes.

The complete legacy BlueSky suite is not currently a clean environment gate:
its TCP fixtures require sandbox-disallowed sockets, its traffic fixtures use
removed NumPy aliases, and the installed OpenAP API lacks
`cruise_mean_vcas`. These failures are separate from the research suite and
must not be reported as envelope regressions.

## Validation status

Licensed BADA 4.2 `A320-232` and BADA 3.15 `A320__` scopes are closed for the
scenarios named above, including configuration policies, lifecycle isolation,
horizontal acceleration, joint horizontal/vertical energy allocation, and
objective route comparisons. The plugin-disabled OpenAP/ISA gate remains
byte-identical to pinned upstream. Unresolved modeling questions—notably
generic non-clean configuration selection and phase-aware speed limits—are
tracked only in `research-modeling-open-issues.md`.
