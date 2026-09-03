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

- `plugin/pyBADA`;
- `plugin/NWP-meteo`;
- `plugin/recorder`;
- combined validation on the future `integration/research-plugins` branch.

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

One plugin-neutral experiment will be executed through these profiles:

1. OpenAP + ISA without recorder;
2. OpenAP + ISA with recorder;
3. OpenAP + ERA5 or GFS with recorder;
4. PyBADA + ISA with recorder;
5. PyBADA + ERA5 or GFS with recorder.

The integration runner must collect a small external sample stream in all five
cases. Where the recorder is enabled, recorder CSV state must agree with the
external sampler at common timestamps. Profiles 1 and 2 must be state-identical
to establish recorder non-interference; this first paired gate now passes for
the fixed 240-step OpenAP/ISA trajectory. The comparison must define fields and
tolerances before viewing differences; exact equality remains required where
the implementation and atmosphere are otherwise identical.

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
simulation-speed ratio. Runtime measurements are operational evidence and
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

The operational example uses the same commands with
`experiments/example_ops.scn` and a separate output directory.

```shell
PYTHONNOUSERSITE=1 PYTHONPATH=. python -m pytest -q tests/research
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
git diff --check
```

Licensed datasets, weather caches, credentials, generated CSV/JSONL/metadata,
and local run manifests remain outside version control.
