# Research plugin reproducibility matrix

## Purpose

This is the authoritative status matrix for standalone and combined research
plugins. A configuration is marked validated only when its complete runtime
combination has objective evidence; component tests do not silently close a
missing end-to-end cell.

The integration branch owns common scenarios, configuration profiles,
orchestration, external state sampling, cross-configuration comparison, and
analysis. Production implementation remains on the corresponding plugin
branch:

- `plugin/pybada-tem`;
- `plugin/NWP-meteo`;
- `plugin/recorder`;
- combined validation on `integration/plugin-stack`.

## Configuration matrix

| Profile | Performance | Atmosphere | Recorder | Current status | Evidence or remaining gate |
| --- | --- | --- | --- | --- | --- |
| `baseline-recorder-free` | OpenAP | ISA | Off | Validated | Byte-identical final state against upstream `22fdf9e3`; SHA-256 `ff0df5d67a954ea88ab8c00e7d052ce81021ee78b2110061d881956fe38930f9` |
| `baseline-recorder` | OpenAP | ISA | On | Validated | 241 external samples are byte-equivalent to recorder-free execution; recorder wrote 240 rows; external-sample SHA-256 `83bb38f343c09658152dac96bf2d5504b67d513ddfbe1381bd2920e31cae5420` |
| `meteo-recorder` | OpenAP | ERA5 | On | Validated | Strict cached 2025-08-15 12Z run: 241 external samples, 240 recorder rows, exact agreement at common timestamps; external-sample SHA-256 `129bd850095dc0572cede40c50cae8c58366d874efc87c2460bee0cebac7e3a5` |
| `meteo-recorder` | OpenAP | GFS | On | Validated | Strict cached 2025-08-15 12Z run: 241 external samples, 240 recorder rows, exact agreement at common timestamps; external-sample SHA-256 `5d0785c398db982be04f01ff4bc035fb11f058a42fb461aa76006a61477705c4` |
| `pybada-recorder` | BADA 3.15 `A320__` | ISA | On | Validated for recorded scope | Licensed acceleration, saturation, energy, turn, envelope, route, and timestep gates pass |
| `pybada-recorder` | BADA 4.2 `A320-232` | ISA | On | Validated for recorded scope | Licensed acceleration, saturation, energy, turn, envelope, route, and timestep gates pass |
| `combined-recorder` | BADA 3.15 `A320__` | ERA5 | On | Validated | 38 samples; REPORT/OFF exact equality; maximum power residual `0.143169 W/kg` |
| `combined-recorder` | BADA 4.2 `A320-232` | ERA5 | On | Validated | 38 samples; REPORT/OFF exact equality; maximum power residual `0.102854 W/kg` |
| `combined-recorder` | BADA 3.15 `A320__` | GFS | On | Validated | 38 samples; REPORT/OFF exact equality; maximum power residual `0.142647 W/kg` |
| `combined-recorder` | BADA 4.2 `A320-232` | GFS | On | Validated | 38 samples; REPORT/OFF exact equality; maximum power residual `0.102525 W/kg` |

The four combined weather cells use strict, automatic, non-interpolated
meteorology and `LONGITUDINAL` envelope checks. They establish only the tested
date, domain, aircraft, datasets, timestep, and short trajectory recorded by
their manifests and metadata.

## Five-profile comparison gate

The plugin-neutral experiment is executed through these profiles:

1. OpenAP + ISA without recorder;
2. OpenAP + ISA with recorder;
3. OpenAP + ERA5 or GFS with recorder;
4. PyBADA + ISA with recorder;
5. PyBADA + ERA5 or GFS with recorder.

The direct matrix completed on 2026-09-03: all five profiles reached
`destination_reached`, recorder non-interference was exact, invalid atmosphere
samples were zero, and configured below-domain ISA was exercised by both ERA5
profiles. The offline suite at that checkpoint reported 230 passed and 5
deselected.

The final clean operational matrix completed on 2026-09-04 against
`experiments/example_ops_full_clean.scn`, source SHA-256
`621ef82cf2efbec39457d873aa41fb458d54456ab1a3d9798d611082370a214f`.
All five profiles were valid and reached `destination_reached`:

| Profile | Simulated duration | Atmosphere samples | Fuel/mass change | Simulation wall time |
| --- | ---: | --- | ---: | ---: |
| `baseline-recorder-free` | 8,037.0 s | ISA 16,075 | 0 kg | 8.615 s |
| `baseline-recorder` | 8,037.0 s | ISA 16,075 | 0 kg | 11.815 s |
| `meteo-recorder` | 7,570.0 s | ERA5 15,141 | 0 kg | 36.955 s |
| `pybada-recorder` | 8,095.5 s | ISA 16,192 | 4,412.152 kg | 158.514 s |
| `combined-recorder` | 7,594.5 s | ERA5 15,190 | 4,200.160 kg | 187.281 s |

Recorder-free and recorded baseline external samples were exactly identical.
All profiles had zero invalid atmosphere samples and zero unexpected fallback
samples. The operational `ATDIST` condition stops before the ERA5 lower
vertical boundary, so this run contains no ERA5-to-ISA transition; it does not
supersede the direct-matrix transition evidence.

Recorder non-interference uses exact time-and-aircraft alignment and has zero
tolerance. Meteorology, performance, combined, interaction, and runtime
differences remain informational: they receive no pass/fail threshold without
an independently justified physical reference. `matrix-summary.json` is the
authoritative structured comparison; `comparisons.csv` is its deterministic
scalar flattening.

## Validated scientific scope

- Python 3.12.13 in the recorded environment;
- pyBADA 0.1.14 with licensed BADA 3.15 `A320__` and BADA 4.2 `A320-232`;
- KINEMATIC observation and TEM dynamics under ISA;
- acceleration/deceleration, thrust saturation, climb/descent joint energy,
  conflicting commands, coordinated turns, mass/fuel integration, route
  capture, flight envelopes, and timestep convergence;
- cached ERA5 and GFS atmospheric state with strict failure semantics, exact
  provider timestamps, no extrapolation, and explicit provenance;
- current recorder schema `samples-v10`, with audited validator compatibility
  back through `samples-v7` according to required capabilities.

This is not evidence for other BADA releases or aircraft, arbitrary weather
products/domains/dates, all simulation timesteps, or numerical equivalence
between different performance or atmospheric models.

## Current closing commands

The scenario-driven matrix runner performs a resource preflight before it
starts any long-running profile. Each profile runs in a fresh process and emits
its rendered scenario, manifest, external samples, recorder artifacts when
enabled, termination reason, wall/CPU timings, simulated duration, and
simulation-speed ratio. The matrix summary also records summed worker wall/CPU
time and orchestration overhead. Runtime measurements are operational evidence and
should be repeated before drawing performance conclusions.

```shell
PYTHONNOUSERSITE=1 PYTHONPATH=. \
  python tests/research/run_profile_matrix.py \
  --scenario experiments/example_direct.scn \
  --config experiments/profiles.json \
  --output output/matrix/example_direct \
  --preflight-only

PYTHONNOUSERSITE=1 PYTHONPATH=. \
  python tests/research/run_profile_matrix.py \
  --scenario experiments/example_direct.scn \
  --config experiments/profiles.json \
  --output output/matrix/example_direct
```

The validated clean operational example uses the same commands with
`experiments/example_ops_full_clean.scn` and a separate output directory.
`experiments/example_ops.scn` preserves the original operational trajectory for
future non-clean configuration work.

```shell
PYTHONNOUSERSITE=1 PYTHONPATH=. \
  python -m pytest tests/research -m "not licensed_bada and not external_weather"
PYTHONNOUSERSITE=1 PYTHONPATH=. \
  python tests/research/run_pybada_revalidation.py --validate-only --skip-unit
PYTHONNOUSERSITE=1 PYTHONPATH=. \
  python tests/research/run_weather_tem_envelope.py --validate-only --skip-unit
PYTHONNOUSERSITE=1 PYTHONPATH=. \
  python tests/research/compare_disabled_baseline.py
PYTHONNOUSERSITE=1 PYTHONPATH=. \
  python tests/research/compare_recorder_noninterference.py
PYTHONNOUSERSITE=1 PYTHONPATH=. \
  python tests/research/run_integration_profile.py --atmosphere ERA5 \
  --workdir /tmp/bluesky-openap-era5 --output /tmp/bluesky-openap-era5.json
PYTHONNOUSERSITE=1 PYTHONPATH=. \
  python tests/research/run_integration_profile.py --atmosphere GFS \
  --workdir /tmp/bluesky-openap-gfs --output /tmp/bluesky-openap-gfs.json
python tests/research/validate_run_manifest.py research-run.example.json
```

The 2026-09-04 offline gate reported 240 passed, 5 deselected, and four known
NumPy 2.5 fixture deprecation warnings.

### ERA5 identity for the clean operational matrix

- simulation date and start: 2025-05-01 12:00 UTC;
- region label: `western-europe`;
- bounds: 40°N, 5°W to 53°N, 10°E;
- pressure levels: 100, 125, 150, 175, 200, 225, 250, 300, 350,
  400, 450, 500, 550, 600, 650, 700, 750, 775, 800, 825, 850,
  875, 900, 925, 950, 975, and 1000 hPa;
- sampled slots: 12:00, 13:00, and 14:00 UTC;
- time interpolation: disabled;
- below-domain policy: configured ISA;
- above, lateral, and time-domain policies: reject;
- sampled cache SHA-256 values: `c6defbe2c37473d0c6bf295c6fb6bae10cbe8c2cf8cdcc034ee379c30e7f3e77`,
  `0a4c27a1f4bbf75659d5c8ecc60b447627866a2e760df426e8be6da231dced47`,
  and `9f434b134785e7ef98c7251f66cef2e653196d5e6c00559e1bcaccb95c030b13`.

Licensed datasets, weather caches, credentials, generated CSV/JSONL/metadata,
and local run manifests remain outside version control.
