# PyBADA, NWP meteorology, and research recorder plugins

The authoritative validation matrix, evidence rules, and execution commands are
in [`reproducibility-matrix.md`](reproducibility-matrix.md). Current validation
status belongs to retained evidence for an exact commit, not to this operator
guide.

For diagrams of the current components, per-tick call order, lifecycle, and
ownership boundary with original BlueSky, start with
[`current-plugin-architecture.md`](current-plugin-architecture.md).

The sole active recorder contract is `samples-v12`. Its exact column order,
units, missing values, metadata, event stream, lifecycle, and exports are
defined in
[`recorder-v12-contract.md`](../recorder/recorder-v12-contract.md). The contract
includes the pre-propagation TEM evaluation state and raw model ROCD. Every
other sample-schema value is rejected by active manifests and validators.

The project environment used for research dependencies is the
`bluesky_research` Conda environment.
Base BlueSky remains usable without any research dependencies.

See [`current-plugin-architecture.md`](current-plugin-architecture.md) for
lifecycle, configuration ownership, failure semantics, and branch boundaries.
See
[`bada-envelope-implementation.md`](../pybada-tem/bada-envelope-implementation.md)
for
the implemented per-aircraft envelope policies, checks, and evidence gates.
```shell
conda activate bluesky_research
python -m pip install -e '.[research,test]'
python -m pip check
```

`pygrib` requires the platform GRIB/ecCodes libraries; `netCDF4` requires the
platform HDF5/NetCDF libraries. Install those with the operating-system package
manager before installing the corresponding extra.

## Configuration

### Recorder settings

| Setting                 | Default  | Meaning                                                                                                      |
|-------------------------|----------|--------------------------------------------------------------------------------------------------------------|
| `research_output_path`  | `output` | Writable root for recorder artifacts. Relative paths are resolved as BlueSky resources.                      |
| `research_recording_dt` | `1.0`    | Recorder update interval in simulation seconds. `RECORDRESEARCH INTERVAL` changes the active timer interval. |

### PyBADA settings

| Setting                     | Default        | Meaning                                                                                                                                            |
|-----------------------------|----------------|----------------------------------------------------------------------------------------------------------------------------------------------------|
| `pybada3_data_path`         | empty          | Folder containing licensed BADA 3 OPF/APF data.                                                                                                    |
| `pybada4_data_path`         | empty          | Licensed BADA 4 model-parent folder.                                                                                                               |
| `pybada_family`             | `4`            | Family selected when the first aircraft requires activation and no `PERFMODEL` command has selected one.                                           |
| `pybada3_version`           | empty          | BADA 3 version passed to pyBADA. Required with the BADA 3 path.                                                                                    |
| `pybada4_version`           | empty          | BADA 4 version passed to pyBADA. Required with the BADA 4 path.                                                                                    |
| `pybada_strict`             | `False`        | When enabled, hold on performance evaluation failures; KINEMATIC mode also holds when the adapter reports an infeasible horizontal thrust request. |
| `pybada_aircraft_aliases`   | `{}`           | Explicit requested-to-dataset aircraft mapping. Strict runs do not use prefix matching or an unapproved dummy.                                     |
| `pybada_speed_schedule`     | `ICAO`         | `ICAO` follows live CAS/Mach intent; `CONSCAS` forces `constCAS`.                                                                                  |
| `pybada_envelope_policy`    | `OFF`          | Creation default: `OFF`, `REPORT`, `ENFORCE`, or `ABORT`.                                                                                          |
| `pybada_envelope_profile`   | `LONGITUDINAL` | Creation default: `CORE_ONLY`, `LONGITUDINAL`, `FULL`, or `CUSTOM`.                                                                                |
| `pybada_envelope_checks`    | `[]`           | Explicit checks used by the `CUSTOM` creation profile. Named profiles expand to their predefined checks.                                           |
| `pybada_configuration_mode` | `PYBADA`       | Creation default: pyBADA-selected configuration or fixed `CRUISE`.                                                                                 |

Licensed datasets stay outside version control. Activating a family requires
both its path and version; loading the plugin with no traffic may defer that
selection. Dataset versions are passed through to pyBADA without a
plugin-maintained allowlist. New aircraft use `TEM` dynamics by default.

### Weather settings

The exact weather settings, defaults, accepted values, and command equivalents
are maintained in
[`README-weather.md`](../nwp-meteo/README-weather.md#settings-reference).
They cover `meteo_strict`, `meteo_below_domain_policy`,
`meteo_time_autoupdate`, `meteo_time_interpolation`, `era5_cache_path`,
`era5_region`, `era5_pressure_levels`, `gfs_cache_path`, `windgfs_source`, and
`windgfs_url`.

Place CDS credentials in the location documented by the CDS API client and
never in this repository. `WINDECMWF` activation checks for credentials even
when its requested files are already cached. Weather caches default to
`cache/weather/era5` and `cache/weather/gfs`. The default GFS source is the
`AWS` URL layout; `NCEI` selects its historical-analysis layout, and actual
archive availability must be verified for the required date. An explicit
`windgfs_url` overrides the selected source's standard base URL.

For result-generating runs set `pybada_strict = True` and
`meteo_strict = True`. Set `meteo_time_autoupdate = False` when a run must be
forbidden from acquiring later time slots; an expired slot then stops a strict
run or produces an explicit ISA fallback in interactive mode. At plugin
activation BlueSky reports the selected BADA
family, resolved data directory, and strict policy. Missing optional packages,
data, or credentials fail before aircraft processing.

`meteo_time_interpolation = False` is the default scientific policy. A file
stamped `T` represents `[T,T+slot)`: one hour for ERA5 and six hours for GFS.
Opting in linearly blends file `T` with the next provider slot throughout that
interval and records both timestamps and the blend fraction as provenance.

## Commands

- `PLUGIN LOAD PYBADATEM`, then `PERFMODEL BADA3|BADA4`,
  `DYNAMICS [acid] KINEMATIC|TEM`, `SPDSCHED ICAO|CONSCAS`, and
  `PERFSTATUS [acid] [CURRENT|BOUNDS|ALL]` for grouped current performance,
  evaluated bounds, model resolution, validity, and miss counts. With no view
  it reports `ALL`; `MAX` and `MAXS` are accepted as aliases for `BOUNDS`.
  In `TEM` mode, BlueSky still supplies selected-speed and waypoint intent,
  while pyBADA owns the applied speed/vertical response, thrust, and fuel
  through the selected energy allocation.
- `TEMPOLICY [acid] [SPEED|VERTICAL|JOINT] [w_accel w_vertical]` selects how a
  TEM aircraft divides specific excess power, `a + (g0/V) w = (Thr - D)/m`,
  between acceleration `a` and geometric vertical rate `w`. `SPEED` (the
  default, recorded as `SPEED_PRIORITY`) tracks speed first and climbs or
  descends at the model's rated-thrust rate, but never faster than guidance
  asks. `VERTICAL` (`VERTICAL_PRIORITY`) tracks the guidance vertical rate first
  and keeps speed at or above the envelope's minimum TAS. `JOINT` minimises
  `w_accel (a - a_req)^2 + w_vertical (w - w_ref)^2` with `a` in m/s2 and `w`
  in m/s; both weights are required and positive, so there is no implicit
  default. Guidance plans speed changes and RTA with the acceleration and
  deceleration the selected policy delivers, not BlueSky's generic `axmax`.
- `SPDSCHED ICAO` is the default. ESF follows each aircraft's live selected
  CAS or Mach representation using BlueSky's current CAS/Mach threshold;
  altitude alone never switches the law. A conflict-resolution-owned TAS
  target selects `constTAS`. `SPDSCHED CONSCAS` explicitly forces `constCAS`.
  Dynamics and vertical-envelope evaluation receive the same captured choice.
  The recorder stores the active law and exactly one original CAS, Mach, or
  resolution-owned TAS target in the v12 evaluation fields. Initial `DIRECT`
  activation preserves raw Mach for PyBADA route guidance.
- `BADACONFIG acid CRUISE|PYBADA` either fixes the addressed aircraft at BADA
  configuration `CR` or delegates configuration selection to pyBADA using
  BlueSky intent and the current operating state. The default is `PYBADA`.
  `MANAGED` is reserved for a possible future plugin state machine.
- `MASS acid,mass_kg` transactionally sets one aircraft's mass. `ENFORCE`
  rejects an infeasible assignment without changing the prior mass; `ABORT`
  applies it, emits a quality event, and places the simulation in HOLD.
- `ENVELOPE` shows the creation default and current aircraft policies;
  `ENVELOPE OFF|REPORT|ENFORCE|ABORT` changes the creation default, and
  `ENVELOPE acid,policy` changes one aircraft transactionally.
  `ENVELOPECHECKS acid,profile[,checks]` selects `CORE_ONLY`, `LONGITUDINAL`,
  `FULL`, or `CUSTOM`; only `CUSTOM` accepts an explicit checklist. Dynamics
  and envelope policy are independent.
- `PLUGIN LOAD WINDECMWF` or `PLUGIN LOAD WINDGFS`, then load a validated
  bounding box with the command of the same name. `WINDGFS lat0,lon0,lat1,lon1`
  derives its cycle from simulation UTC; append `YYYYMMDD,00|06|12|18` to
  select an explicit, reproducible analysis cycle. `METEOCONFIG` inspects or
  changes `STRICT`, `BELOW`, `TIMEUPDATE`, and `INTERPOLATION`;
  `METEOSTATUS lat,lon,alt` inspects one provider sample. Full syntax is in the
  [weather guide](../nwp-meteo/README-weather.md#runtime-commands).
- `PLUGIN LOAD RESEARCHRECORDER`, then
  `RECORDRESEARCH START run.csv`, `STATUS`, `STOP`, `RESET`, or
  `INTERVAL seconds`. The default sampling interval is one simulation second.
  `ATMOSSTATUS [acid]` shows the applied temperature, pressure, density,
  pressure altitude, wind, airspeed, provenance, and corresponding ISA values.
- `EXPORTRESEARCH` attempts Excel, KML, and altitude-plot exports from the most
  recently stopped authoritative CSV. Stop the recorder before exporting;
  optional export failures do not modify the CSV.

`RECORDRESEARCH START` first finalizes an active recording, then opens the
requested CSV and matching event file for writing. Reusing a filename truncates
those existing files, and the matching metadata is replaced when the new run
is stopped. The legacy `SAVEMETEO`, `SAVEATMOS`, `SAVEHEADER`, and `SAVETRAJ`
commands all toggle the same recorder and remain compatibility aliases only;
new scenarios should use `RECORDRESEARCH`.

ERA5 selects the latest hourly slot and GFS the latest six-hour analysis
cycle (00, 06, 12, or 18 UTC) at or before simulation UTC. Data outside the
requested horizontal bounds or source vertical domain are never extrapolated.
Interactive runs retain ISA with an explicit reason; strict runs abort.
Each ERA5 request contains only the selected slot and requested bounds. Domains
crossing the antimeridian are retrieved as two bounded CDS requests and merged
on a common validated vertical grid.

On a cache miss, the provider terminal reports the requested dataset slot,
ERA5 area and pressure levels or GFS source URL, and the cache destination.
It reports again after the downloaded file has passed validation and has been
accepted into the cache.

### Preparing the weather cache

The standalone downloaders use the same deterministic names and validation as
the simulation plugins. They validate and reuse a matching cached file; a
missing or invalid file is downloaded to a `.part` path and atomically renamed
only after validation. Use `--dry-run` first to inspect the URL and target
without network access:

```shell
python scripts/download_era5.py 20260817 12 40 -5 45 5 --dry-run
python scripts/download_gfs.py 20250815 12 --until 20250815T18 --dry-run
```

Remove `--dry-run` to populate the cache. ERA5 requires to be configured CDS API
credentials. `--cache PATH` overrides the default; configure the matching
`era5_cache_path` or `gfs_cache_path` in BlueSky when using an override.

## Validation

The dependency-free suite, licensed PyBADA revalidation, ERA5/GFS by BADA 3/4
TEM envelope gate, and plugin-disabled upstream comparison are defined in
[`reproducibility-matrix.md`](reproducibility-matrix.md). Licensed and external
weather gates require local resources and may be skipped by a normal test run.
Their presence does not assert a result for the current commit.

```shell
python -m pytest tests/research -m 'not licensed_bada and not external_weather'
python -m pytest tests/research -m licensed_bada --run-manifest research-run.local.json
python -m pytest tests/research -m external_weather --run-manifest research-run.local.json
git diff --check
```

The local manifest records external paths and credentials by reference and is
ignored by Git. Validate it before use with
`python tests/research/validate_run_manifest.py research-run.local.json`.
Replace the example's all-zero commit and external-resource paths with the
actual full revision and local resources before running a gate.
Generated CSV is authoritative; metadata uses the matching versioned JSON
schema. Optional Excel, KML, and plots must be derived from the finalised CSV and
do not alter it. See the
[v12 recorder contract](../recorder/recorder-v12-contract.md) for the exact
artefact interface.

Every recorder-produced TEM CSV must also pass the aligned numerical audit:

```shell
python tests/research/validate_numerical_run.py output/run.csv \
  --output output/run.numerical-audit.json
```

The matrix and PyBADA revalidation runners invoke this automatically and fail
when the audit fails. The audit checks geometric point-mass balance and
one-step TAS, altitude, and mass updates against the v12 evaluation state; it
does not establish observed-flight accuracy.

### Timestep-convergence study

TEM results carry a first-order discretisation error that depends on the
simulation timestep. Before relying on a campaign's results, show that the
chosen timestep is converged for that campaign's scenario:

```shell
python tests/research/run_convergence_study.py research/my-route \
  --observable fuel_burn_kg --production-dt 0.05 --tolerance 5
```

The tool writes one variant of the scenario per timestep to
`output/convergence/<scenario>/<scenario>-dtNNN.scn` (any `DT` line replaced,
the recorder output renamed to `<scenario>-dtNNN.csv`), runs each in a fresh
detached process, applies the generic evidence and numerical audit to each run,
and compares them per aircraft. It prints one line per aircraft and writes the
full result to `output/convergence/<scenario>/report.json`; the exit status is
non-zero if any check fails.

The scenario must contain exactly one `RECORDRESEARCH START`, and its
`RECORDRESEARCH INTERVAL` must be a whole multiple of every timestep studied.
Give it relative to `scenario/` without `.scn`, or as an absolute path.

| Option | Meaning |
|---|---|
| `--dt A B C` | Three timesteps with one constant ratio; default `0.10 0.05 0.025`. |
| `--fields ...` | Recorded fields to compare; default altitude (geometric and pressure), TAS, mass, latitude and longitude. |
| `--exact ACID:FIELD ...` | Fields that must agree at every timestep, e.g. a constant-rate climb. |
| `--observable NAME` | `fuel_burn_kg` or `final:<field>`, extrapolated to its zero-timestep limit. |
| `--production-dt S` | The timestep the campaign uses; must be one of the three. |
| `--tolerance X` | Maximum estimated observable error at `--production-dt`, in its units. |
| `--order-band LOW HIGH` | Accepted observed order on smooth segments; default `0.7 1.3`. |
| `--validate-only` | Re-analyse existing variant outputs without running them. |
| `--jobs N` | Variants run concurrently; default 3. |
| `--pybada-nonstrict` | Pass `--pybada-nonstrict` to each run, e.g. for kinematic baselines. |

For each aircraft and field the report gives the error against the finest run
and a status:

- `first order` (with its observed order `p`): the order measured on the
  smooth segment before the aircraft's first speed capture or level-off lies
  in the band;
- `resolved`: differences are below the resolution of about 1 cm of altitude
  or position, 0.1 mm/s of TAS, or 10 g of fuel, so there is no order to
  measure;
- `exact` or `not exact` for fields given with `--exact`;
- `order outside band`: a failure.

Independently of the status, the whole-run error must shrink at least as
order 0.5 predicts, and capture and level-off times must agree within three
steps plus the recorder cadence. The observable entry reports its values, its
observed order, the extrapolated limit and, with `--production-dt`, the
estimated error there. Choose the observable and tolerance from the quantity
the campaign reports.

The same tool runs the fixed gates by name: `pybada-convergence-bada3`,
`pybada-convergence-bada4` (both in the PyBADA revalidation runner) and
`era5-convergence-bada4` (in the weather runner). Passing them shows the
integration converges in the regimes they isolate; it is not evidence for
another route.

A simulation in HOLD has not thereby completed its experiment. Strict
performance failures and rejected runtime fuel/mass updates stop propagation
before the rejected tick changes position. An envelope `ABORT` event captures
the triggering state and finalises recorder artefacts synchronously, but the
result is still an aborted partial run. Manifests and campaign validators must
reject held or partial evidence unless that exact abort is the test objective.

The supplied licensed scenarios cover configuration observation and policy,
horizontal acceleration and saturation, joint climb/descent allocation,
conflicting commands, turn load and energy, envelopes, lifecycle, routes, and
timestep comparison. Their expected behaviour and validator catalogue are
described in
[`bada-envelope-implementation.md`](../pybada-tem/bada-envelope-implementation.md).
Generated
values and pass/fail status remain with the local manifest and evidence set.

At the default one-second interval, a `samples-v12` CSV writes 3,600 rows per
aircraft-hour. Measure the average row length in the produced CSV and multiply
by 3,600 and the aircraft count to estimate storage before a long experiment.
