#!/usr/bin/env python3
"""Validate three licensed BADA timestep runs (dt100, dt050, dt020).

dt100, dt050, and dt020 denote 0.10, 0.05, and 0.02 second BlueSky base
timesteps. All runs must have a common 0.10 second recorder cadence.
"""

import argparse
import csv
import json
import math
from pathlib import Path


G0 = 9.80665
EXPECTED_DT = {'dt100': 0.10, 'dt050': 0.05, 'dt020': 0.02}
# Fixed before licensed evidence was inspected. These bounds accommodate one
# common recorder quantum at capture and first-order state integration, while
# remaining small relative to operational changes in the scenario.
TRAJECTORY_LIMITS = {'geometric_alt_m': 1.0, 'pressure_alt_m': 1.0,
                     'tas_m_s': 0.15, 'mass_kg': 0.15}
MONOTONIC_NOISE = {'geometric_alt_m': 0.02, 'pressure_alt_m': 0.02,
                   'tas_m_s': 0.005, 'mass_kg': 0.005}


def _number(row, field):
    value = float(row[field])
    if not math.isfinite(value):
        raise ValueError(f'{row.get("acid", "?")} {field} is non-finite')
    return value


def _true(row, field):
    return row.get(field, '').lower() == 'true'


def _rms(values):
    return math.sqrt(sum(value * value for value in values) / len(values))


def _load(path, family, label):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = sorted(csv.DictReader(stream), key=lambda row: _number(row, 'sim_time_s'))
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    errors = []
    expected_aircraft = {'3': 'A320__', '4': 'A320-232'}[family]
    expected_acid = f'B{family}CV'
    expected_scenario = f'pybada-convergence-bada{family}-{label}'
    if metadata.get('schema_version') != 'samples-v10':
        errors.append(f'{label}: metadata schema is not samples-v10')
    if metadata.get('scenario') != expected_scenario:
        errors.append(f'{label}: scenario is {metadata.get("scenario")!r}')
    if metadata.get('sample_intervals_s') != [0.1]:
        errors.append(f'{label}: recorder interval is not exactly 0.10 s')
    if not math.isclose(float(metadata.get('base_timestep_s', -1)),
                        EXPECTED_DT[label], abs_tol=1e-12):
        errors.append(f'{label}: base timestep is not {EXPECTED_DT[label]:.2f} s')
    if metadata.get('event_total') not in (None, 0):
        errors.append(f'{label}: quality events were recorded')
    event_path = path.with_suffix('.events.jsonl')
    if event_path.exists() and event_path.stat().st_size:
        errors.append(f'{label}: event file is not empty')
    if len(rows) < 500 or {row.get('acid') for row in rows} != {expected_acid}:
        errors.append(f'{label}: unexpected row count or aircraft identity')
    checks = {
        'performance_model': f'PYBADATEM-BADA{family}',
        'performance_dataset_version': {'3': '3.15', '4': '4.2'}[family],
        'performance_aircraft': expected_aircraft, 'performance_dummy': 'False',
        'performance_valid': 'True', 'performance_miss_count': '0',
        'dynamics_mode': 'TEM'}
    for field, expected in checks.items():
        values = {row.get(field) for row in rows}
        if values != {expected}:
            errors.append(f'{label}: {field} values are {sorted(values)}')

    conflict, recovery, stable = [], [], []
    residuals, motion_errors, mass_bound_errors = [], [], []
    for row in rows:
        thrust = _number(row, 'thrust_n')
        lower, upper = sorted((_number(row, 'idle_thrust_n'),
                               _number(row, 'maximum_thrust_n')))
        if not lower - 1e-6 <= thrust <= upper + 1e-6:
            errors.append(f'{label}: thrust is outside licensed bounds')
            break
        if _number(row, 'fuel_flow_kg_s') < 0 or _number(row, 'mass_kg') <= 0:
            errors.append(f'{label}: non-physical fuel flow or mass')
            break
        if (row.get('energy_allocation_policy') == 'BADA_ESF'
                and _number(row, 'requested_acceleration_m_s2') < -0.01
                and _number(row, 'requested_vertical_rate_m_s') > 0.1
                and _number(row, 'applied_acceleration_m_s2') > 0
                and _number(row, 'applied_vertical_rate_m_s') > 0.1):
            conflict.append(row)
        if (row.get('energy_allocation_policy') == 'HORIZONTAL_ADAPTED'
                and _number(row, 'requested_acceleration_m_s2') < -0.01
                and _number(row, 'applied_acceleration_m_s2') < 0
                and abs(_number(row, 'applied_vertical_rate_m_s')) <= 1e-9):
            recovery.append(row)
        if (_number(row, 'sim_time_s') > 100 and _true(row, 'speed_capture')
                and abs(_number(row, 'vertical_speed_m_s')) <= 1e-6):
            stable.append(row)
    for previous, current in zip(rows, rows[1:]):
        elapsed = _number(current, 'sim_time_s') - _number(previous, 'sim_time_s')
        if not math.isclose(elapsed, 0.1, abs_tol=1e-8):
            errors.append(f'{label}: timestamps do not share a 0.10 s cadence')
            continue
        observed_burn = (_number(previous, 'mass_kg') -
                         _number(current, 'mass_kg'))
        endpoint_burns = [elapsed * _number(row, 'fuel_flow_kg_s')
                          for row in (previous, current)]
        lower, upper = min(endpoint_burns), max(endpoint_burns)
        mass_bound_errors.append(max(lower - observed_burn,
                                     observed_burn - upper, 0.0))
        if current in conflict:
            tas = _number(current, 'tas_m_s')
            temp_factor = ((288.15 - 0.0065 * _number(current, 'pressure_alt_m')) /
                           _number(current, 'temperature_k'))
            available = ((_number(current, 'thrust_n') - _number(current, 'drag_n')) *
                         tas / _number(previous, 'mass_kg'))
            allocated = (tas * _number(current, 'applied_acceleration_m_s2') +
                         G0 * _number(current, 'applied_vertical_rate_m_s') / temp_factor)
            residuals.append(abs(available - allocated))
            observed_ax = ((_number(current, 'tas_m_s') - _number(previous, 'tas_m_s')) /
                           elapsed)
            observed_vs = ((_number(current, 'geometric_alt_m') -
                            _number(previous, 'geometric_alt_m')) / elapsed)
            motion_errors.extend((abs(observed_ax - _number(
                current, 'applied_acceleration_m_s2')),
                                  abs(observed_vs - _number(
                current, 'applied_vertical_rate_m_s'))))
    if len(conflict) < 50 or len(recovery) < 10 or len(stable) < 20:
        errors.append(f'{label}: insufficient conflict/recovery/stable evidence '
                      f'({len(conflict)}/{len(recovery)}/{len(stable)})')
    if max(residuals, default=math.inf) > 0.75:
        errors.append(f'{label}: energy residual exceeds 0.75 W/kg')
    if max(motion_errors, default=math.inf) > 0.15:
        errors.append(f'{label}: sampled motion mismatch exceeds 0.15 SI units')
    # The common 0.10 s recorder cadence subsamples dt050/dt020 integration.
    # The exact substep sum cannot be reconstructed from endpoint flows, so
    # require observed burn to lie within their local flow bounds instead.
    if max(mass_bound_errors, default=math.inf) > 1e-5:
        errors.append(f'{label}: mass loss is outside endpoint fuel-flow bounds')
    final = rows[-1] if rows else None
    if final and (abs(_number(final, 'geometric_alt_m') - 3657.6) > 0.02
                  or abs(_number(final, 'vertical_speed_m_s')) > 1e-6
                  or not _true(final, 'speed_capture')
                  or abs(_number(final, 'tas_m_s') -
                         _number(final, 'target_tas_m_s')) > 0.02):
        errors.append(f'{label}: final altitude/speed capture is incorrect')
    times = {round(_number(row, 'sim_time_s'), 8): row for row in rows}
    altitude_capture = min((_number(row, 'sim_time_s') for row in rows
                            if abs(_number(row, 'geometric_alt_m') - 3657.6) <= 0.02
                            and abs(_number(row, 'vertical_speed_m_s')) <= 1e-6),
                           default=math.inf)
    speed_capture = min((_number(row, 'sim_time_s') for row in rows
                         if _true(row, 'speed_capture') and
                         _number(row, 'sim_time_s') > 5.0), default=math.inf)
    metrics = {'rows': len(rows), 'times': times, 'altitude_capture': altitude_capture,
               'speed_capture': speed_capture, 'max_energy_residual': max(residuals),
               'max_motion_error': max(motion_errors),
               'max_mass_bound_error': max(mass_bound_errors)}
    return errors, metrics


def validate(paths, family):
    errors, runs = [], {}
    for label, path in paths.items():
        run_errors, runs[label] = _load(path, family, label)
        errors.extend(run_errors)
    common = sorted(set.intersection(*(set(run['times']) for run in runs.values())))
    if len(common) < 500:
        errors.append(f'only {len(common)} exact common timestamps')
    comparison = {}
    reference = runs['dt020']['times']
    for label in ('dt100', 'dt050'):
        comparison[label] = {}
        for field, limit in TRAJECTORY_LIMITS.items():
            differences = [abs(_number(runs[label]['times'][time], field) -
                               _number(reference[time], field)) for time in common]
            maximum, rms = max(differences), _rms(differences)
            comparison[label][field] = (maximum, rms)
            if maximum > limit:
                errors.append(f'{label}: max {field} difference {maximum:.6f} exceeds {limit}')
    for field in TRAJECTORY_LIMITS:
        coarse_max, coarse_rms = comparison['dt100'][field]
        medium_max, medium_rms = comparison['dt050'][field]
        noise = MONOTONIC_NOISE[field]
        if medium_max > coarse_max + noise or medium_rms > coarse_rms + noise:
            errors.append(f'dt050 {field} error is worse than dt100 beyond noise allowance')
    for capture in ('altitude_capture', 'speed_capture'):
        reference_time = runs['dt020'][capture]
        if not math.isfinite(reference_time):
            errors.append(f'{capture} is absent from the fine reference')
            continue
        capture_errors = {}
        for label in ('dt100', 'dt050'):
            capture_errors[label] = abs(runs[label][capture] - reference_time)
            # Capture is a discontinuous event observed on a 0.10 s recorder
            # grid. Permit three base steps plus floating-point slack.
            if capture_errors[label] > 3 * EXPECTED_DT[label] + 1e-8:
                errors.append(f'{label}: {capture} differs excessively from dt020')
        if capture_errors['dt050'] > capture_errors['dt100'] + 0.1 + 1e-8:
            errors.append(f'dt050 {capture} error is worse than dt100')
    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(dict.fromkeys(errors))
    fields = '; '.join(
        f'{field}: dt100 max/rms={comparison["dt100"][field][0]:.6f}/'
        f'{comparison["dt100"][field][1]:.6f}, dt050 max/rms='
        f'{comparison["dt050"][field][0]:.6f}/{comparison["dt050"][field][1]:.6f}'
        for field in TRAJECTORY_LIMITS)
    return (f'VALID: BADA {family} timestep convergence; common={len(common)}, '
            f'altitude captures={[runs[x]["altitude_capture"] for x in EXPECTED_DT]}, '
            f'speed captures={[runs[x]["speed_capture"] for x in EXPECTED_DT]}; {fields}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--family', choices=('3', '4'), required=True)
    parser.add_argument('--dt100', required=True)
    parser.add_argument('--dt050', required=True)
    parser.add_argument('--dt020', required=True)
    args = parser.parse_args(argv)
    try:
        result = validate({label: getattr(args, label) for label in EXPECTED_DT}, args.family)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, ZeroDivisionError) as exc:
        result = f'INVALID evidence:\n  - evidence could not be validated: {exc}'
    print(result)
    return int(result.startswith('INVALID'))


if __name__ == '__main__':
    raise SystemExit(main())
