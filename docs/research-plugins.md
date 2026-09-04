# PyBADA, NWP meteorology, and research recorder plugins

The authoritative tested configuration matrix is
[`reproducibility-matrix.md`](reproducibility-matrix.md). Dated checkpoint and
`next-session` documents preserve development history; they are not current
operator instructions.

For diagrams of the current components, per-tick call order, lifecycle, and
ownership boundary with original BlueSky, start with
[`current-plugin-architecture.md`](current-plugin-architecture.md).

The validated local environment is the `bluesky_research` Conda environment.
Base BlueSky remains usable without any research dependencies.

See [`current-plugin-architecture.md`](current-plugin-architecture.md) for
lifecycle, configuration ownership, failure semantics, and branch boundaries.
See [`bada-envelope-implementation.md`](bada-envelope-implementation.md) for
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

Set `pybada3_data_path` to the folder containing BADA 3 OPF/APF files and
`pybada4_data_path` to the BADA 4 model-parent folder. Licensed datasets stay
outside version control. Activating a family requires both its path and version;
loading the plugin with no traffic may defer that selection. Set
`pybada_aircraft_aliases` to an explicit dictionary;
strict runs never use prefix matching or an unapproved dummy. The version is
passed through to pyBADA without a plugin-maintained allowlist.

Place CDS credentials in the location documented by the CDS API client (never
in this repository). Weather caches default to `cache/weather/era5` and
`cache/weather/gfs` and can be moved with `era5_cache_path` and
`gfs_cache_path`. Recording output uses
`research_output_path` and must be writable.

`windgfs_source` selects NOAA's public `AWS` bucket (the default) or `NCEI`.
Use AWS for the operational 1-degree analysis objects; unlike the former NCEI
Grid 3 URL, the AWS archive includes the verified 2025-08-15 cycles used by the
research scenarios. NCEI reorganized its archive into grid-specific object-store
paths and does not currently expose those 2025 cycles. An explicit
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
  it reports `ALL`; `MAXS` is accepted as an alias for `BOUNDS`.
  In `TEM` mode, pyBADA owns vertical performance while BlueSky retains its
  native horizontal selected-speed and waypoint-speed capture.
- `BADACONFIG acid CRUISE|PYBADA` either fixes the addressed aircraft at BADA
  configuration `CR` or delegates configuration selection to pyBADA using
  BlueSky intent and the current operating state. The default is `PYBADA`.
  `MANAGED` is reserved for a possible future plugin state machine.
- `ENVELOPE [acid] [OFF|REPORT|ENFORCE|ABORT]` controls the addressed
  aircraft's selected-envelope policy. `ENVELOPECHECKS acid
  CORE_ONLY|LONGITUDINAL|FULL|CUSTOM <checks>` controls its expanded check set.
  Dynamics and envelope policy are independent.
- `PLUGIN LOAD WINDECMWF` or `PLUGIN LOAD WINDGFS`, then load a validated
  bounding box with the command of the same name. `WINDGFS lat0,lon0,lat1,lon1`
  derives its cycle from simulation UTC; append `YYYYMMDD,00|06|12|18` to
  select an explicit, reproducible analysis cycle.
- `PLUGIN LOAD RESEARCHRECORDER`, then
  `RECORDRESEARCH START run.csv`, `STATUS`, `STOP`, `RESET`, or
  `INTERVAL seconds`. The default sampling interval is one simulation second.
  `ATMOSSTATUS [acid]` shows the applied temperature, pressure, density,
  pressure altitude, wind, airspeeds, provenance, and corresponding ISA values.

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

Remove `--dry-run` to populate the cache. ERA5 requires configured CDS API
credentials. `--cache PATH` overrides the default; configure the matching
`era5_cache_path` or `gfs_cache_path` in BlueSky when using an override.

## Validation

The current dependency-free suite, complete licensed PyBADA revalidation,
ERA5/GFS by BADA 3/4 TEM envelope gate, and plugin-disabled upstream comparison
are listed in [`reproducibility-matrix.md`](reproducibility-matrix.md). ERA5
and GFS transition, opt-in interpolation, strict/interactive policy, cache, and
matched TEM evidence gates pass for the documented 2025-08-15 datasets.

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
schema. Optional Excel, KML, and plots must be derived from the closed CSV and
do not alter it.

Licensed BADA 4.2 `A320-232` evidence covers CR, IC, AP, TO, and LD across
observation, REPORT, ENFORCE, and ABORT. Licensed BADA 3.15 `A320__`
full-envelope, lifecycle, and route validation is also closed. Both scopes are
summarized in [`bada-envelope-implementation.md`](bada-envelope-implementation.md).
Runtime-derived licensed values and generated evidence remain local and ignored.

Licensed acceleration/deceleration, thrust saturation, joint climb/descent
energy allocation, conflicting-command recovery, turn-load drag, turn energy,
and timestep-convergence gates pass for both documented BADA families. Exact
scenario counts, residuals, and claim limits are recorded in the matrix and
dated checkpoints.

At the default one-second interval, a `samples-v10` CSV writes 3,600 rows per
aircraft-hour. Measure the average row length in the produced CSV and multiply
by 3,600 and the aircraft count to estimate storage before a long experiment.
