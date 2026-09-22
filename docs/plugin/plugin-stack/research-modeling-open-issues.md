# Research modelling open issues

This file records durable model and experiment-design limitations. It contains
no campaign result and is not evidence that an upstream model or dependency is
defective.

## Pressure altitude and geometric altitude

pyBADA evaluates atmospheric and ceiling quantities in pressure-altitude
coordinates, while BlueSky propagates geometric altitude. TEM converts model
ROCD to a geometric vertical rate before propagation and records both pressure
and geometric evaluation state.

The maximum-altitude envelope boundary still needs an explicit non-ISA
coordinate convention. A model ceiling must not be compared with or applied to
geometric altitude until that conversion is defined and tested.

## Nominal speed-law intent

PYBADATEM follows live selected CAS/Mach intent and uses `constTAS` only when
conflict resolution owns the TAS channel. Dynamics and vertical bounds receive
the same choice. This is a nominal ESF selection, not replay of a transient
optimiser.

The current scenario-generation layer can lose source climb/descent CAS and
Mach phase intent by emitting CAS at every waypoint. Preserving `CLB_CAS`,
`CLB_MACH`, `DES_MACH`, and `DES_CAS` requires the planned `samples-v12`
speed-law field and scenario-generation work; it must not be added under the
v11 schema name.

## Non-clean BADA configuration management

`BADACONFIG PYBADA` delegates aerodynamic-configuration selection to pyBADA
using BlueSky phase intent and the current operating state. `BADACONFIG CRUISE`
fixes configuration `CR`. The plugin does not yet own a generic phase-aware
configuration state machine covering take-off, initial climb, approach,
landing, and landing-gear transitions.

A full-flight scenario forced to `CR` must therefore keep its state within the
clean envelope; doing so is a test-fixture constraint, not a general model of
terminal flight. The lower-speed source fixture in
`experiments/example_ops.scn` must not be adapted by changing its intent merely
to satisfy clean limits. Supporting it requires generic non-clean configuration
management and new validation.

## Phase-aware speed and Mach checks

The stock `LONGITUDINAL` profile applies minimum and maximum CAS and Mach checks
throughout the flight. The implementation does not yet establish whether every
model-returned Mach bound is appropriate for every phase and aerodynamic
configuration.

Experiments may select a deliberate `CUSTOM` check set, but that is a declared
scope restriction rather than validation that omitted limits are irrelevant.
Future work should define and validate phase/configuration-dependent selection
without encoding one aircraft's behaviour as a generic rule.

## High-altitude energy feasibility

A commanded cruise altitude is not necessarily reachable for an arbitrary
aircraft, mass, speed law, weather state, and timestep. Strict TEM correctly
rejects non-finite or unbounded model output, but that guard does not establish
whether a failed climb was physically infeasible, poorly guided, or affected by
configuration and speed-law selection.

High-altitude campaign scenarios therefore require an explicit feasibility
question, recorded mass and speed-law intent, envelope checks, timestep study,
and a completion criterion. No general maximum operational altitude should be
inferred from a single route run.

## Arrival and safety-HOLD semantics

`ATDIST ..., HOLD` proves only horizontal proximity. `ATALT ..., HOLD` proves
only altitude proximity, and two independent conditions are not equivalent to
one compound condition because either can hold the simulation first.

A route experiment that claims destination arrival needs a compound horizontal
and vertical completion rule. Until one exists, the manifest and validator must
state exactly which component was tested. A separate safety HOLD must occur
after the expected run and within the prepared weather horizon; reaching that
safety guard is failure, not successful completion.

## Timestep convergence

The implementation provides multi-timestep comparison scenarios and a
convergence validator. Their existence does not make a chosen timestep
converged for every route or operating regime. Each campaign must establish an
acceptable observable and tolerance before using convergence as a scientific
claim.
