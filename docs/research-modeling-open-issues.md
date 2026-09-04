# Research modeling open issues

This file records observations that need later design or validation work. It is
not evidence that the underlying model or dependency is defective.

## Non-clean BADA configurations

The current operational reproducibility matrix uses a clean (`CR`) aerodynamic
configuration throughout the flight. `experiments/example_ops_full_clean.scn`
therefore raises the initial and early-climb speeds to 200 kt so that the
licensed BADA4 A320-232 can be created and propagated within the selected clean
CAS envelope.

The original operational trajectory is preserved in
`experiments/example_ops.scn`, including its lower terminal speeds. Supporting
that trajectory faithfully requires a generic, phase-aware implementation of
non-clean configurations (at least take-off, initial climb, approach, landing,
and landing-gear state) rather than changing the source trajectory to fit the
clean envelope.

Known observation: with envelope policy `ENFORCE` and the clean configuration,
creation at 130.4 kt was rejected as below the BADA4 A320-232 minimum CAS. A
180 kt trial was also rejected at the default creation mass; 200 kt was
accepted. This behavior should be revisited with phase-aware configurations.

## Minimum-speed and Mach checks

The stock `LONGITUDINAL` envelope applies both minimum CAS and minimum Mach to
the complete flight. During low-altitude creation, the BADA cruise minimum Mach
made an otherwise plausible terminal CAS infeasible. The clean operational
profiles currently use an explicit set of mass, CAS, altitude, ROC, and ROD
checks and omit Mach checks.

Future work should determine whether Mach bounds are phase/configuration
dependent and implement that selection generically. Do not interpret the
current workaround as validation that the pyBADA limit itself is wrong.

## FL390 behavior

With the original FL390 cruise segment, strict TEM propagation progressively
lost CAS during climb. One run reached the three-hour safety hold at about
9,537 m while still en route. With a longer guard, it reached about 11,806 m at
55.18 m/s TAS and pyBADA returned an unbounded climb rate of 102.66 m/s; strict
mode correctly held the simulation.

The clean test scenario currently uses FL350 and reaches its destination. Later
work should determine whether FL390 is infeasible for this mass and schedule,
whether the speed/altitude guidance needs different energy management, or
whether phase/configuration-specific envelope logic changes the result. No
conclusion has been established yet.

## Arrival condition

All experiment scenarios use a one-nautical-mile destination condition:

```text
00:00:00.00>ATDIST EXAMPLE, <destination latitude>, <destination longitude>, 1.0, HOLD
```

This is a consistent horizontal completion guard and avoids an altitude-only
condition firing after an abnormal descent far from the destination. It does
not prove vertical arrival: an earlier PyBADA diagnostic run entered the
one-nautical-mile circle while still thousands of metres above the destination.
That limitation must remain visible in the evidence and should later be
replaced by a true compound arrival condition requiring both horizontal and
vertical proximity. Separate `ATDIST` and `ATALT` commands are not equivalent
to a compound condition because either command can place the simulation in
HOLD independently.

The independent safety hold is 03:30:00. It prevents an unbounded run while
leaving margin beyond the roughly three-hour expected flight and remaining
inside the cached ERA5 time horizon.

## Greenwich-crossing meteorology cubes

This issue is resolved and covered by a regression test. Regional longitude
axes crossing Greenwich were previously wrapped to `[0, 360)` and sorted
linearly. A grid such as `[-5, 0, 5, 10]` consequently became
`[0, 5, 10, 355]`, which falsely represented the small Greenwich crossing as a
large missing interval.

`bluesky/plugins/meteo/cube.py` now places the axis break at the largest
unrepresented circular gap, yielding a continuous axis such as
`[355, 360, 365, 370]`. Query longitudes below the new axis origin are shifted
by 360 degrees before interpolation. The regression test verifies valid samples
on both sides of Greenwich and rejection of a genuinely out-of-domain point.
