# Research-plugin documentation map

This branch keeps eight substantive maintained plugin documents plus this index.
Historical session notes and implementation plans are intentionally excluded;
Git history preserves their provenance without presenting them as current
guidance.

| Need                                                      | Authoritative document                                                            |
|-----------------------------------------------------------|-----------------------------------------------------------------------------------|
| Install and operate the plugins                           | [`research-plugins.md`](research-plugins.md)                                      |
| Understand host hooks, lifecycle, ownership, and branches | [`current-plugin-architecture.md`](current-plugin-architecture.md)                |
| Implement or consume recorder output                      | [`recorder-v12-contract.md`](../recorder/recorder-v12-contract.md)                |
| Review PyBADA envelopes and validation gates              | [`bada-envelope-implementation.md`](../pybada-tem/bada-envelope-implementation.md) |
| Execute scenarios and interpret evidence                  | [`reproducibility-matrix.md`](reproducibility-matrix.md)                          |
| Review unresolved model questions                         | [`research-modeling-open-issues.md`](research-modeling-open-issues.md)            |
| Review explicitly deferred plugin work                    | [`plugin-future-work.md`](plugin-future-work.md)                                  |
| Prepare ERA5/GFS cache data                               | [`README-weather.md`](../nwp-meteo/README-weather.md)                             |

`../../../research-run.example.json` is a schema-valid template, not an active local
configuration. Licensed datasets, weather cache files, credentials, generated
evidence, and `research-run.local.json` remain outside version control.

## Branch disposition

- `plugin/recorder`: standalone recorder implementation and tests;
- `plugin/NWP-meteo`: standalone meteorology implementation and cache tools;
- `plugin/pybada-tem`: standalone PyBADA/TEM implementation and tests;
- `integration/plugin-stack`: reviewed composition, inert-hook gate, scenarios,
  orchestration, comparison, CI, and maintained technical documentation.

Maintained cross-plugin technical instructions live on
`integration/plugin-stack`.
