# Preparing weather data

Run these commands from the repository root. Preparing the cache before a long
simulation avoids waiting for downloads during the run.

## Operational checklist

For a reproducible run:

1. Set the simulation `DATE` to an available historical UTC time.
2. Determine the complete route bounds and latest possible simulation time.
3. Run the matching downloader with `--dry-run`, then without it.
4. Run the cache validator before starting BlueSky.
5. In the scenario, set `STRICT`, `TIMEUPDATE`, and `INTERPOLATION` explicitly.
6. Load the provider only after setting `DATE`, then query one known point with
   `METEOSTATUS`.
7. Record the run and validate the resulting CSV and metadata.

The normal scientific configuration is:

```text
METEOCONFIG STRICT ON
METEOCONFIG TIMEUPDATE ON
METEOCONFIG INTERPOLATION OFF
```

This configuration advances through prepared slots but stops instead of using
ISA when required weather is missing or the aircraft leaves the requested
domain. Enable interpolation only when the experiment intentionally models
intermediate meteorological states.

## ERA5

Choose the smallest bounding box that contains the complete expected route and
add a safety margin. Specify the first analysis hour and the last hour that the
simulation can reach. For a run from 2025-08-15 12:00 through 18:00 UTC:

```shell
python scripts/download_era5.py 20250815 12 40 -5 45 5 \
  --until 20250815T18 --region western-europe --dry-run
python scripts/download_era5.py 20250815 12 40 -5 45 5 \
  --until 20250815T18 --region western-europe
```

The first command only lists the cache files. The second downloads missing or
invalid files. Valid files already in `cache/weather/era5` are reused. ERA5
requires CDS API credentials.

Then configure and run BlueSky:

```text
DATE 15,8,2025,12:00:00
PLUGINS LOAD WINDECMWF
WINDECMWF 40,-5,45,5
```

The region label is for humans. The cache filename also contains the UTC slot,
readable pressure range, and a digest of the exact CDS request. The request
digest covers the precise bounds, discrete pressure levels, variables, product
type, and formats. Cached NetCDF contents are validated against the requested
slot, levels, coverage, units, variables, and dimensions before reuse.

Prefer one bounding box covering the complete route. Independently downloaded
adjacent boxes are not automatically joined: their native horizontal values
come from the same ERA5 field, but independent vertical resampling can produce
slightly different height axes. Crossing between boxes therefore requires a
validated overlap rather than assuming edge continuity.

After the downloads finish, validate every hourly file without network access:

```shell
python tests/research/validate_era5_cache.py \
  20250815T12 20250815T18 40 -5 45 5 --region western-europe
```

Then run the matched BADA 4 TEM integration gate and validate its recording:

```text
IC research/era5-tem-envelope
```

```shell
python tests/research/validate_era5_tem_run.py output/era5-tem-envelope.csv
```

Test automatic use of the next cached hourly slot:

```text
IC research/era5-tem-transition
```

```shell
python tests/research/validate_era5_transition_run.py output/era5-tem-transition.csv
```

Validate strict and interactive failure policy directly against the cache:

```shell
python tests/research/validate_era5_policies.py
```

Test explicitly enabled temporal interpolation:

```text
IC research/era5-tem-interpolation
```

```shell
python tests/research/validate_era5_interpolation_run.py output/era5-tem-interpolation.csv
```

## GFS

GFS files contain the global grid, so geographic bounds are applied when the
plugin validates and samples the data rather than during download. Prepare all
six-hour analysis cycles that the run may reach:

```shell
python scripts/download_gfs.py 20250815 12 --until 20250815T18 --dry-run
python scripts/download_gfs.py 20250815 12 --until 20250815T18
```

The default source is NOAA's public AWS GFS bucket. The former NCEI Grid 3 URL
returns 404 for these cycles after NCEI's grid-specific archive reorganization;
do not select `--source NCEI` for the August 2025 research run. Valid files in
`cache/weather/gfs` are reused.

Validate the downloaded 12Z and 18Z files at the scenario test point:

```shell
python tests/research/validate_gfs_cache.py \
    20250815T12 20250815T18 41.3 2.1 3048
```

Then exercise the exact six-hour transition with licensed BADA 4 TEM:

```text
IC research/gfs-tem-transition
```

After the scenario holds, validate its recorded evidence:

```shell
python tests/research/validate_gfs_transition_run.py \
    output/gfs-tem-transition.csv
```

Test explicitly enabled six-hour temporal interpolation:

```text
IC research/gfs-tem-interpolation
```

```shell
python tests/research/validate_gfs_interpolation_run.py \
    output/gfs-tem-interpolation.csv
```

Exercise GFS strict and interactive failure policies without network access:

```shell
python tests/research/validate_gfs_policies.py
```

## Time policy

`meteo_time_autoupdate = True` is the default. At a slot boundary the active
provider loads the next cached file or downloads it, validates it, and only
then applies it. ERA5 slots are hourly; GFS slots are six-hourly.

Experiment profiles define meteorological domain behavior explicitly:

```json
"domain_policy": {
  "below": "ISA",
  "above": "REJECT",
  "lateral": "REJECT",
  "time": "REJECT"
}
```

`below` accepts `REJECT` or plain `ISA`. Plain ISA is evaluated at the actual
aircraft altitude, uses zero wind, and records the configured source transition;
it is not an extrapolation of the lowest weather level. `ISA_ANCHORED` is
reserved and currently fails explicitly as not implemented. The other three
boundaries accept only `REJECT`.

`meteo_time_interpolation = False` is the default. A file stamped `T` supplies
the entire interval from `T` up to, but excluding, the next slot. Set it to
`True` only when linear temporal interpolation is scientifically intended; the
provider then requires the next file too and reports both timestamps and the
blend fraction in provenance.

Scenarios must record these choices explicitly for reproducible runs:

```text
METEOCONFIG STRICT ON
METEOCONFIG TIMEUPDATE ON
METEOCONFIG INTERPOLATION OFF
```

Run `METEOCONFIG` without arguments to show the active policy.

Set `meteo_time_autoupdate = False` to forbid acquisition during a run. At the
next slot boundary the old dataset expires instead of being used for a new
time. Strict mode stops the run; interactive mode switches to ISA with a
`TIME_SLOT_EXPIRED` reason. Pre-caching alone does not disable automatic time
updates: keep the setting enabled to advance through the prepared files.

## Inspecting a point

After loading a provider, query latitude, longitude, and altitude with:

```text
METEOSTATUS 41.3,2.1,10000
```

BlueSky parses the altitude using its normal altitude input convention. The
result reports the resolved altitude in metres, validity, dataset time, north
and east wind, temperature, pressure, and density. `ATMOSSTATUS` instead shows
the atmosphere actually applied to aircraft and requires
`PLUGINS LOAD RESEARCHRECORDER`.

## Failure interpretation

| Condition | Strict mode | Interactive mode |
| --- | --- | --- |
| Outside requested bounds or vertical domain | Stop | ISA with explicit spatial fallback |
| New slot missing or invalid with `TIMEUPDATE ON` | Stop | ISA with `TIME_SLOT_UNAVAILABLE` |
| Slot boundary with `TIMEUPDATE OFF` | Stop | ISA with `TIME_SLOT_EXPIRED` |
| Interpolation enabled but successor missing | Stop | ISA with `TIME_SLOT_UNAVAILABLE` |

No mode retains an expired or failed weather cube. A result-generating run is
valid only when its evidence contains the intended source and slots, no
fallback reason, and no performance misses.
