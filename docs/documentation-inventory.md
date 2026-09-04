# Research documentation map

This branch keeps five substantive maintained documents plus this index. Historical session notes and
implementation plans are intentionally excluded; Git history preserves their
provenance without presenting them as current guidance.

| Need | Authoritative document |
| --- | --- |
| Install and operate the plugins | `research-plugins.md` |
| Understand host hooks, lifecycle, ownership, and branches | `current-plugin-architecture.md` |
| Review PyBADA envelopes and licensed validation scope | `bada-envelope-implementation.md` |
| Reproduce scenarios and interpret comparisons | `reproducibility-matrix.md` |
| Review unresolved model questions | `research-modeling-open-issues.md` |
| Prepare ERA5/GFS cache data | `../scripts/README-weather.md` |

`../research-run.example.json` is a schema-valid template, not an active local
configuration. Licensed datasets, weather cache files, credentials, generated
evidence, and `research-run.local.json` remain outside version control.

## Branch disposition

- `plugin/recorder`: standalone recorder implementation and tests;
- `plugin/NWP-meteo`: standalone meteorology implementation and cache tools;
- `plugin/pybada-tem`: standalone PyBADA/TEM implementation and tests;
- `integration/plugin-stack`: reviewed composition and inert-hook gate;
- `research/reproducibility`: scenarios, orchestration, comparison, and CI;
- `docs/research-consolidation`: maintained documentation assembled here.
