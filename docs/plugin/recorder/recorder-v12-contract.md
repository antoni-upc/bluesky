# `samples-v12` recorder contract

## Scope

`samples-v12` is the sole supported research-recorder schema. Every CSV row and
its metadata file must identify that exact version; manifests and validators
reject every other value. The CSV is the authoritative numerical record. JSONL
events and JSON metadata accompany it, while Excel, KML, and PNG files are
optional derived views.

For a requested `run.csv`, the recorder uses these paths:

| Artifact        | Path                             | Role                                                          |
|-----------------|----------------------------------|---------------------------------------------------------------|
| Samples         | `run.csv`                        | Authoritative, ordered `samples-v12` rows.                    |
| Events          | `run.events.jsonl`               | Synchronously flushed quality events emitted while recording. |
| Metadata        | `run.metadata.json`              | Finalized run summary and exact column/unit description.      |
| Derived exports | `run.xlsx`, `run.kml`, `run.png` | Optional views created only by `EXPORTRESEARCH`.              |

## Changes from the previous schema

- Columns 57 to 60 (`evaluation_speed_evolution` and the three
  `evaluation_speed_target_*` columns) are new. The previous columns 57 to 86 keep
  their names and order and move to positions 61 to 90.
- `maximum_altitude_m` is now defined as a pressure-altitude ceiling. The
  `ALTITUDE_MAX` check compares it with pressure altitude; the previous schema
  compared it with geometric altitude.
- The metadata `columns` list grows from 86 to 90 entries.

Files written with the previous schema are not valid `samples-v12` files and
are not upgraded in place.

## Recording lifecycle

`RECORDRESEARCH START run.csv` finalizes any active recording and then opens
the requested CSV and event stream in write mode. Reusing a name truncates the
CSV and event file. Metadata is written or replaced only when the new recording
is finalised, so consumers must not inspect an active path as completed
evidence; an older metadata file may still be present until finalisation.

Each recorder update writes one row per aircraft and flushes the CSV. No traffic
produces no data row. `RECORDRESEARCH STOP` closes the streams and writes
metadata. `RESET` first performs the same finalisation when active and then
forgets the recorder path and counters.

A subscribed quality event is ignored while the recorder is inactive. While it
is active, the event is written and flushed before control returns to the
publisher. An event whose action is `ABORTED` additionally samples the current
aircraft state and finalises CSV, JSONL, and metadata before the publisher
places the simulation in HOLD. That artefact remains an aborted partial run,
not successful experiment completion.

## CSV representation

The file is UTF-8 CSV with a header. Column order is normative and fixed below.
Numerical values that are non-finite, missing optional plugin arrays, and `None`
values are represented by an empty CSV field. Booleans are serialized as
`True` or `False`. Timestamps are ISO 8601 strings supplied by the simulation or
provider. Selected checks and failed checks are comma-separated enum names.

The units column uses `-` for identifiers, text, booleans, timestamps, and
counts; `1` denotes a dimensionless numerical quantity.

|  # | Column                            | Unit   | Meaning                                                                                                                |
|---:|-----------------------------------|--------|------------------------------------------------------------------------------------------------------------------------|
|  1 | `schema_version`                  | -      | Literal `samples-v12`.                                                                                                 |
|  2 | `run_id`                          | -      | Stem of the requested CSV filename.                                                                                    |
|  3 | `sim_time_s`                      | s      | BlueSky simulation time at sampling.                                                                                   |
|  4 | `sample_interval_s`               | s      | Active recorder timer interval, or empty when unavailable.                                                             |
|  5 | `sim_utc`                         | -      | Simulation UTC timestamp.                                                                                              |
|  6 | `acid`                            | -      | Aircraft identifier.                                                                                                   |
|  7 | `actype`                          | -      | Requested BlueSky aircraft type.                                                                                       |
|  8 | `lat_deg`                         | deg    | Applied latitude.                                                                                                      |
|  9 | `lon_deg`                         | deg    | Applied longitude.                                                                                                     |
| 10 | `geometric_alt_m`                 | m      | Applied geometric altitude.                                                                                            |
| 11 | `pressure_alt_m`                  | m      | Applied pressure altitude; geometric altitude is the standalone fallback when no atmosphere hook supplies it.          |
| 12 | `tas_m_s`                         | m/s    | Applied true airspeed.                                                                                                 |
| 13 | `cas_m_s`                         | m/s    | Applied calibrated airspeed.                                                                                           |
| 14 | `mach`                            | 1      | Applied Mach number.                                                                                                   |
| 15 | `vertical_speed_m_s`              | m/s    | Applied geometric vertical speed.                                                                                      |
| 16 | `heading_deg`                     | deg    | Aircraft heading.                                                                                                      |
| 17 | `track_deg`                       | deg    | Ground track.                                                                                                          |
| 18 | `temperature_k`                   | K      | Applied atmospheric temperature.                                                                                       |
| 19 | `pressure_pa`                     | Pa     | Applied atmospheric pressure.                                                                                          |
| 20 | `density_kg_m3`                   | kg/m^3 | Density recomputed from the applied atmospheric state.                                                                 |
| 21 | `wind_north_m_s`                  | m/s    | Applied northward wind component.                                                                                      |
| 22 | `wind_east_m_s`                   | m/s    | Applied eastward wind component.                                                                                       |
| 23 | `atmosphere_source`               | -      | Applied source, such as `ISA`, `ERA5`, or `GFS`.                                                                       |
| 24 | `atmosphere_valid`                | -      | Traffic atmosphere-validity flag; false identifies an invalid provider sample even when the host applies ISA fallback. |
| 25 | `dataset_time`                    | -      | Provider slot or interpolation provenance; empty for ISA.                                                              |
| 26 | `fallback_reason`                 | -      | Explicit atmosphere fallback reason, otherwise empty.                                                                  |
| 27 | `performance_model`               | -      | Selected performance implementation; PyBADA includes its BADA family.                                                  |
| 28 | `performance_dataset_version`     | -      | Active performance-dataset version when exposed.                                                                       |
| 29 | `performance_aircraft`            | -      | Resolved performance-dataset aircraft name.                                                                            |
| 30 | `performance_resolution`          | -      | Aircraft-resolution method reported by the model store.                                                                |
| 31 | `performance_dummy`               | -      | Whether resolution selected a dummy model.                                                                             |
| 32 | `dynamics_mode`                   | -      | `TEM`, `KINEMATIC`, or empty for a provider without this interface.                                                    |
| 33 | `performance_valid`               | -      | Inverse of the selected performance provider's current invalid flag.                                                   |
| 34 | `performance_miss_count`          | -      | Cumulative performance evaluation-failure count.                                                                       |
| 35 | `thrust_n`                        | N      | Current thrust used or reported by the selected performance model.                                                     |
| 36 | `required_thrust_n`               | N      | Unclamped thrust requirement associated with the requested or model energy response.                                   |
| 37 | `rated_thrust_n`                  | N      | Model-rated thrust returned for the operating condition.                                                               |
| 38 | `idle_thrust_n`                   | N      | Evaluated idle-thrust bound.                                                                                           |
| 39 | `maximum_thrust_n`                | N      | Evaluated maximum-thrust bound.                                                                                        |
| 40 | `drag_n`                          | N      | Evaluated aerodynamic drag.                                                                                            |
| 41 | `fuel_flow_kg_s`                  | kg/s   | Fuel flow corresponding to the recorded applied thrust.                                                                |
| 42 | `mass_kg`                         | kg     | Applied aircraft mass at sampling.                                                                                     |
| 43 | `target_tas_m_s`                  | m/s    | Resolved true-airspeed target.                                                                                         |
| 44 | `requested_acceleration_m_s2`     | m/s^2  | Requested horizontal acceleration.                                                                                     |
| 45 | `applied_acceleration_m_s2`       | m/s^2  | Thrust-feasible applied horizontal acceleration.                                                                       |
| 46 | `thrust_limited`                  | -      | Whether the evaluated or allocated response encountered a thrust bound.                                                |
| 47 | `thrust_limitation_reason`        | -      | Thrust-bound or allocation reason, otherwise empty.                                                                    |
| 48 | `speed_capture`                   | -      | Whether the applied speed step captures its target.                                                                    |
| 49 | `requested_vertical_rate_m_s`     | m/s    | Signed vertical rate requested from the performance evaluation.                                                        |
| 50 | `applied_vertical_rate_m_s`       | m/s    | Signed vertical rate retained by the energy allocator.                                                                 |
| 51 | `evaluation_tas_m_s`              | m/s    | Pre-propagation TAS supplied to the TEM evaluation.                                                                    |
| 52 | `evaluation_alt_m`                | m      | Pre-propagation geometric altitude supplied to the TEM evaluation.                                                     |
| 53 | `evaluation_mass_kg`              | kg     | Pre-propagation mass supplied to the TEM evaluation.                                                                   |
| 54 | `evaluation_temperature_k`        | K      | Pre-propagation temperature supplied to the TEM evaluation.                                                            |
| 55 | `evaluation_pressure_alt_m`       | m      | Pre-propagation pressure altitude supplied to the TEM evaluation.                                                      |
| 56 | `evaluation_timestep_s`           | s      | Timestep used for the recorded TEM response.                                                                           |
| 57 | `evaluation_speed_evolution`      | -      | Captured `constCAS`, `constM`, or `constTAS` law for this evaluation.                                                  |
| 58 | `evaluation_speed_target_cas_m_s` | m/s    | Original selected CAS target when represented as CAS.                                                                  |
| 59 | `evaluation_speed_target_mach`    | 1      | Original selected Mach target when represented as Mach, including `CONSCAS`.                                           |
| 60 | `evaluation_speed_target_tas_m_s` | m/s    | Active conflict-resolution TAS target.                                                                                 |
| 61 | `model_rocd_m_s`                  | m/s    | Raw model ROCD before geometric-altitude propagation correction and final energy allocation.                           |
| 62 | `energy_share_factor`             | 1      | Model energy-share factor.                                                                                             |
| 63 | `energy_allocation_policy`        | -      | Applied allocation: `SPEED_PRIORITY`, `VERTICAL_PRIORITY`, or `JOINT` for TEM rows.                                    |
| 64 | `propulsion_bank_angle_deg`       | deg    | Effective bank angle supplied to propulsion/performance evaluation.                                                    |
| 65 | `propulsion_load_factor`          | 1      | Load factor supplied to propulsion/performance evaluation.                                                             |
| 66 | `envelope_policy`                 | -      | Effective per-aircraft `OFF`, `REPORT`, `ENFORCE`, or `ABORT` policy.                                                  |
| 67 | `envelope_profile`                | -      | Effective envelope profile name.                                                                                       |
| 68 | `envelope_checks`                 | -      | Comma-separated effective check names.                                                                                 |
| 69 | `envelope_status`                 | -      | Aggregated current envelope status.                                                                                    |
| 70 | `envelope_failed_checks`          | -      | Comma-separated contributing failed checks.                                                                            |
| 71 | `envelope_last_action`            | -      | Most recent envelope action.                                                                                           |
| 72 | `envelope_last_reason`            | -      | Reason associated with the most recent emitted quality event.                                                          |
| 73 | `envelope_event_count`            | -      | Cumulative quality-event count for the aircraft.                                                                       |
| 74 | `envelope_violation_count`        | -      | Cumulative non-empty envelope-reason count for the aircraft.                                                           |
| 75 | `mass_min_kg`                     | kg     | Evaluated minimum mass bound.                                                                                          |
| 76 | `mass_max_kg`                     | kg     | Evaluated maximum mass bound.                                                                                          |
| 77 | `envelope_configuration`          | -      | Configuration used for flight-envelope bounds.                                                                         |
| 78 | `minimum_cas_m_s`                 | m/s    | Evaluated minimum CAS bound.                                                                                           |
| 79 | `maximum_cas_m_s`                 | m/s    | Evaluated maximum CAS bound.                                                                                           |
| 80 | `minimum_mach`                    | 1      | Evaluated minimum Mach bound.                                                                                          |
| 81 | `maximum_mach`                    | 1      | Evaluated maximum Mach bound.                                                                                          |
| 82 | `maximum_altitude_m`              | m      | Current-state pressure-altitude ceiling; compare with `pressure_alt_m`, not `geometric_alt_m`.                         |
| 83 | `minimum_rocd_m_s`                | m/s    | Evaluated signed minimum vertical-rate bound.                                                                          |
| 84 | `maximum_rocd_m_s`                | m/s    | Evaluated signed maximum vertical-rate bound.                                                                          |
| 85 | `envelope_lateral_configuration`  | -      | Configuration used for lateral bounds.                                                                                 |
| 86 | `bank_angle_deg`                  | deg    | Effective commanded turn-bank angle.                                                                                   |
| 87 | `load_factor`                     | 1      | Coordinated level-turn load factor derived from the effective bank angle.                                              |
| 88 | `minimum_load_factor`             | 1      | Evaluated minimum load-factor bound.                                                                                   |
| 89 | `maximum_load_factor`             | 1      | Evaluated maximum load-factor bound.                                                                                   |
| 90 | `maximum_bank_angle_deg`          | deg    | Evaluated maximum bank-angle bound.                                                                                    |

Columns 51 through 56 and column 61 are the numerical-audit inputs. They
capture the state actually used for the TEM evaluation rather than reconstructing that
state from the post-propagation row. A TEM CSV is not accepted solely because
its header is correct; it must also pass `validate_numerical_run.py`.

Columns 57 through 60 capture the speed decision made once per PyBADA
evaluation, before propagation. The same decision is used for the dynamics
and the vertical-envelope evaluation of that step. When the law is recorded,
exactly one target column is populated:

| Selection                                  | `evaluation_speed_evolution` | Populated target                  |
|--------------------------------------------|------------------------------|-----------------------------------|
| Selected CAS                               | `constCAS`                   | `evaluation_speed_target_cas_m_s` |
| Selected Mach                              | `constM`                     | `evaluation_speed_target_mach`    |
| Selected Mach under `SPDSCHED CONSCAS`     | `constCAS`                   | `evaluation_speed_target_mach`    |
| Active conflict-resolution TAS target      | `constTAS`                   | `evaluation_speed_target_tas_m_s` |

`SPDSCHED CONSCAS` takes precedence over a conflict-resolution TAS target.
All four columns are empty when PyBADA did not evaluate the aircraft in that
step or the evaluation failed, and for every other performance provider.

## Event JSONL contract

Each non-empty line is one JSON object with these keys:

| Key            | Representation                                                                              |
|----------------|---------------------------------------------------------------------------------------------|
| `aircraft`     | Aircraft identifier.                                                                        |
| `component`    | Producer name; current PyBADA events use `PYBADATEM`.                                       |
| `reason`       | Failed-check or quality reason, including comma-separated combined reasons when applicable. |
| `policy`       | Effective producer policy.                                                                  |
| `action`       | Applied action such as `ACCEPTED`, `REJECTED`, `LIMITED`, or `ABORTED`.                     |
| `continuation` | `CONTINUE`, or `STOP` for an aborted event.                                                 |
| `requested`    | Requested scalar/object, or `null`.                                                         |
| `applied`      | Applied scalar/object, or `null`.                                                           |
| `sim_time_s`   | Simulation time, or `null` when unavailable.                                                |

Event records do not contain a separate `schema_version`. Their run association
and contract come from the metadata `events_jsonl` path and its
`schema_version`. Events are synchronously flushed, and only events emitted
while recording are part of the artefact.

The run-level `quality_status` starts as `VALID`. The first non-abort event
changes it to `DEGRADED`; later non-abort events do not restore it. An
`ABORTED` event changes it to `ABORTED`.

## Metadata contract

The finalised JSON object has these top-level keys:

| Key                  | Meaning                                                                                                               |
|----------------------|-----------------------------------------------------------------------------------------------------------------------|
| `schema_version`     | Literal `samples-v12`.                                                                                                |
| `run_id`             | CSV filename stem, matching every row.                                                                                |
| `created_utc`        | Wall-clock UTC timestamp captured at recorder start.                                                                  |
| `rows`               | Number of written aircraft rows.                                                                                      |
| `csv`                | Recorded CSV path.                                                                                                    |
| `python`             | Python version used by the process.                                                                                   |
| `base_timestep_s`    | BlueSky base simulation timestep observed at finalization.                                                            |
| `dependencies`       | Installed versions of `numpy`, `scipy`, `openap`, `pyBADA`, `netCDF4`, and `pygrib`; unavailable packages are `null`. |
| `scenario`           | BlueSky scenario name at finalization.                                                                                |
| `sample_intervals_s` | Sorted distinct recorder intervals observed while sampling.                                                           |
| `atmosphere_sources` | Sorted distinct non-empty sources observed in rows.                                                                   |
| `dataset_times`      | Sorted distinct non-empty provider-time provenance observed in rows.                                                  |
| `columns`            | Exact ordered list of the 90 CSV columns above.                                                                       |
| `missing_value`      | Literal `empty CSV field`.                                                                                            |
| `units`              | Mapping for columns that carry physical units or dimensionless numerical values.                                      |
| `events_jsonl`       | Associated event-stream path.                                                                                         |
| `event_total`        | Number of events written during the run.                                                                              |
| `reason_totals`      | Mapping from event reason string to count.                                                                            |
| `quality_status`     | Final `VALID`, `DEGRADED`, or `ABORTED` status.                                                                       |
| `effective_envelope` | Finalization-time per-aircraft envelope/configuration snapshot.                                                       |

Each `effective_envelope` item contains `aircraft`, `policy`,
`configuration_mode`, `configuration`, `high_lift_id`, `landing_gear`,
`minimum_limit_name`, `maximum_limit_name`, `minimum_load_factor`,
`maximum_load_factor`, `maximum_bank_angle_deg`, and `checks`. It describes
aircraft still present at finalisation; it is not a lifecycle history.

Consumers must verify at least the schema version, exact `columns` list, row
count, intended atmosphere sources and provider times, quality status, and
scenario-specific completion rule. Metadata presence alone does not turn a
held, aborted, or otherwise partial run into valid campaign evidence.

## Derived exports

`EXPORTRESEARCH` requires a stopped recording whose CSV still exists. It
independently attempts:

- an Excel workbook containing the CSV table;
- an absolute-altitude KML line string per aircraft;
- a simulation-time versus geometric-altitude PNG per aircraft.

Each export reports success or failure separately. These files can depend on
optional packages, are not inputs to validation, and never replace or modify
the authoritative CSV.
