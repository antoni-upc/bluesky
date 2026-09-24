# Research plugin validation protocol

## Purpose

This document defines the validation matrix, evidence requirements, and commands
for the research-plugin stack. It does not record campaign results. A gate is
current evidence only after it has been executed against the exact clean commit
named by its run manifest and all required artefacts have been retained outside
the integration branch.

The integration branch owns common scenarios, profiles, orchestration,
external-state sampling, cross-configuration comparison, and validators.
Production implementation remains owned by the corresponding plugin branch:

- `plugin/recorder`;
- `plugin/NWP-meteo`;
- `plugin/pybada-tem`;
- combined validation on `integration/plugin-stack`.

## Status vocabulary

Documentation uses these terms deliberately:

| Term                 | Meaning                                                                                                                                                       |
|----------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Implemented          | Production code and a callable validation interface exist.                                                                                                    |
| Dependency-free gate | The gate runs without licensed BADA data or external weather files.                                                                                           |
| External gate        | The gate requires resources declared by a local run manifest. Normal tests may skip it.                                                                       |
| Revalidated          | A particular clean commit and resource set passed its complete gate, with retained artifacts. This status belongs in that evidence set, not in this document. |

Unit or component coverage does not establish an end-to-end matrix cell, and a
historical result does not revalidate a newer commit.

## Configuration matrix

| Profile or combination   | Performance | Atmosphere  | Recorder | Gate type                                       | Required acceptance                                                                                                      |
|--------------------------|-------------|-------------|----------|-------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------|
| `baseline-recorder-free` | OpenAP      | ISA         | Off      | Dependency-free plus pinned-upstream comparison | Scenario completes normally; plugin-disabled state is byte-identical to the pinned upstream result.                      |
| `baseline-recorder`      | OpenAP      | ISA         | On       | Dependency-free                                 | External samples are exactly equal to the recorder-free run; recorder artifacts satisfy `samples-v12`.                   |
| `meteo-recorder`         | OpenAP      | ERA5        | On       | External weather                                | Applied source, slots, bounds, strict policy, provenance, and recorder/external sample alignment all match the manifest. |
| `meteo-recorder`         | OpenAP      | GFS         | On       | External weather                                | Same requirements as ERA5, using the selected GFS analysis cycles.                                                       |
| `pybada-recorder`        | BADA 3      | ISA         | On       | Licensed BADA                                   | Model resolution, force/fuel/mass behavior, envelope behavior, completion, and the v12 numerical audit all pass.         |
| `pybada-recorder`        | BADA 4      | ISA         | On       | Licensed BADA                                   | Same requirements as BADA 3 for the declared BADA 4 dataset and aircraft.                                                |
| `combined-recorder`      | BADA 3      | ERA5 or GFS | On       | Licensed BADA plus external weather             | Both component contracts, their interaction validator, normal completion, and the numerical audit pass.                  |
| `combined-recorder`      | BADA 4      | ERA5 or GFS | On       | Licensed BADA plus external weather             | Same requirements as BADA 3 for the declared BADA 4 dataset and aircraft.                                                |

`experiments/profiles.json` supplies the five profile names used by the matrix
runner. The profile configuration is an experiment input, not evidence that a
run completed. Family-, aircraft-, weather-, and date-specific variants must be
declared in the generated manifest.

## Five-profile comparison

The plugin-neutral experiment renders the same source scenario through:

1. OpenAP + ISA without the recorder;
2. OpenAP + ISA with the recorder;
3. OpenAP + ERA5 or GFS with the recorder;
4. PyBADA + ISA with the recorder;
5. PyBADA + ERA5 or GFS with the recorder.

Recorder non-interference is an exact comparison: externally sampled state is
aligned by simulation time and aircraft and must have zero difference.
Differences caused by selecting another atmosphere or performance model are
reported but are not scientific pass/fail criteria without an independently
defined physical reference.

The runner writes `matrix-summary.json` as the structured comparison and
`comparisons.csv` as its deterministic scalar projection. It also records
termination, simulated duration, process timing, and simulation-speed metrics.
Timing is operational telemetry only and must be regenerated in the target
environment before drawing performance conclusions.

## Evidence requirements

A result-generating run must preserve:

- the full integration commit and pinned upstream base;
- a clean-working-tree flag;
- the source scenario and its rendered profile scenario;
- simulation UTC, timestep, duration guard, and random seed;
- exact plugin configuration and recorder interval;
- licensed-dataset and weather-cache identities by reference;
- provider bounds, time policy, interpolation policy, and fallback policy;
- process exit status and explicit termination reason;
- external samples and all authoritative recorder artefacts;
- validator names, outputs, and generated artefact hashes.

`research-run.example.json` is a schema-valid template whose placeholder commit
and paths must be replaced. `research-run.local.json`, licensed datasets,
credentials, weather caches, and generated evidence remain outside version
control.

Validate a local manifest before running an external gate:

```shell
python tests/research/validate_run_manifest.py research-run.local.json
```

## Common acceptance rules

Every completed evidence set must satisfy all applicable rules:

1. Resource preflight succeeds before a long-running profile starts.
2. The manifest names the exact clean commit under test.
3. The process exits normally and the scenario-specific completion condition is
   reached before its safety HOLD, unless the gate explicitly tests a rejection
   or ABORT transition.
4. Every recorder CSV and metadata file declares exact `samples-v12`, the
   metadata column list matches the CSV header, and metadata row/event totals
   match their files.
5. Intended atmosphere sources and dataset times are present; unexpected
   invalid samples or fallback reasons reject a strict result.
6. Performance resolution is non-dummy, required values are finite, and no
   unexpected performance miss occurs.
7. Every TEM recording passes `validate_numerical_run.py` using the recorded
   pre-propagation evaluation state.
8. Quality status and termination agree with the scenario objective. A held or
   aborted artefact is partial unless that precise rejection or ABORT behaviour
   is the gate being tested.

The matrix establishes behaviour only for the commit, scenario, resources,
aircraft, timestep, and domain recorded in the evidence. It is not evidence for
other BADA releases or aircraft, arbitrary weather products or dates, all
timesteps, or physical equivalence between different models.

## Commands

Run the dependency-free suite first:

```shell
python -m pytest tests/research \
  -m "not licensed_bada and not external_weather"
```

Preflight and run the five-profile matrix in fresh child processes:

```shell
python tests/research/run_profile_matrix.py \
  --scenario experiments/example_direct.scn \
  --config experiments/profiles.json \
  --output output/matrix/example_direct \
  --preflight-only

python tests/research/run_profile_matrix.py \
  --scenario experiments/example_direct.scn \
  --config experiments/profiles.json \
  --output output/matrix/example_direct
```

`experiments/example_ops_full_clean.scn` is an alternative clean-configuration
fixture. `experiments/example_ops.scn` preserves the lower-speed operational
source fixture for future phase-aware non-clean work. Neither scenario carries
a validation status until its outputs pass the rules above.

Run the focused gates as applicable:

```shell
python tests/research/run_pybada_revalidation.py
python tests/research/run_weather_tem_envelope.py
python tests/research/compare_disabled_baseline.py
python tests/research/compare_recorder_noninterference.py
python -m pytest tests/research -m licensed_bada \
  --run-manifest research-run.local.json
python -m pytest tests/research -m external_weather \
  --run-manifest research-run.local.json
```

The PyBADA revalidation runner also runs expected-failure gates. For example,
`pybada-envelope-lateral-strict70` passes only if its 70° REPORT turn reaches an
unbounded TEM output and the detached runner exits with `UNPLANNED HOLD`. Its
log is kept as `output/<scenario>.runner.log` for `--validate-only` runs.

Use `--validate-only --skip-unit` with the two revalidation runners only when
the existing outputs were generated from the same declared commit and resource
set. Otherwise, rerun their scenarios. The disabled-baseline comparison requires
the pinned upstream commit to be present locally; the external gates require
their licensed or weather resources. The focused revalidation runners do not
create an integration run manifest; retain their commit, resource identities,
validator output, and artefact hashes explicitly before assigning evidence
status.

Individual ERA5/GFS cache, transition, interpolation, policy, envelope, route,
energy, and convergence validators remain available under `tests/research/` for
diagnosis and focused evidence. Their successful execution proves only their
declared scope.
