# Research-plugin future work

This backlog is technical integration scope. It contains no campaign result or
paper claim.

## Next schema baseline

`samples-v11` remains frozen as the only supported baseline. The next schema
change is `samples-v12`, reserved for an explicit recorded speed-law field and
scenario generation that preserves source `CLB_CAS`, `CLB_MACH`, `DES_MACH`,
and `DES_CAS` intent instead of emitting CAS at every waypoint. No v12 field is
to be added under the v11 name.

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
