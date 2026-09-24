# Research-plugin future work

This backlog is technical integration scope. It contains no campaign result or
paper claim.

## Active schema baseline

`samples-v12` is the only supported baseline. It records the speed law and
original CAS, Mach, or conflict-resolution TAS target for each evaluation.
Campaign tooling must preserve the v12 contract. Speed-intent scenarios built
from external source trajectories are kept outside the repository; the
repository covers the speed-intent path with portable unit tests only.

## Model and integration work

- Resolve the pressure-altitude versus geometric-altitude convention for
  maximum-altitude bounds under non-ISA weather.
- Expose state-dependent acceleration capability to guidance so waypoint
  planning does not assume the generic `PerfBase.axmax` value.
- Establish timestep convergence rather than treating the selected timestep
  as converged.
- Consider per-tick memoisation of identical envelope evaluations, keyed by
  the complete evaluation state.
- Keep `constTAS` compatible for resolution-owned targets; campaign expansion
  should prioritize correct CAS/Mach intent.

Collision detection and resolution remain disabled and outside the research
scope. Multi-aircraft rollback after a held tick is also not implemented;
partial held artifacts must not be treated as completed runs.
