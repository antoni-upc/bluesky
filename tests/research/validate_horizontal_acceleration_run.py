#!/usr/bin/env python3
"""Validate a licensed level-flight PYBADATEM acceleration gate."""

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path


def _number(row, field):
    value = float(row[field])
    if not math.isfinite(value):
        raise ValueError(f'{row.get("acid", "?")} {field} is non-finite')
    return value


def validate(path, family, acceleration_threshold=0.05, balance_tolerance=0.03):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    errors = []
    scenario = f'pybada-acceleration-bada{family}'
    expected_model = f'PYBADATEM-BADA{family}'
    expected_aircraft = {'3': 'A320__', '4': 'A320-232'}[family]
    if metadata.get('schema_version') != 'samples-v9':
        errors.append('metadata schema is not samples-v9')
    if metadata.get('scenario') != scenario:
        errors.append(f'metadata scenario is not {scenario}')
    intervals = metadata.get('sample_intervals_s')
    if (not isinstance(intervals, list) or len(intervals) != 1 or
            not 0.0 < float(intervals[0]) <= 1.0):
        errors.append(f'sample intervals are not one fixed interval <= 1 s: {intervals}')

    by_acid = defaultdict(list)
    for row in rows:
        by_acid[row.get('acid')].append(row)
    expected_acids = {f'B{family}AK', f'B{family}AT'}
    if set(by_acid) != expected_acids:
        errors.append(f'aircraft are {sorted(by_acid)}, expected {sorted(expected_acids)}')

    summaries = []
    for acid in sorted(expected_acids):
        samples = sorted(by_acid[acid], key=lambda item: _number(item, 'sim_time_s'))
        if len(samples) < 25:
            errors.append(f'{acid} has only {len(samples)} samples')
            continue
        if {row.get('performance_model') for row in samples} != {expected_model}:
            errors.append(f'{acid} did not remain on {expected_model}')
        if {row.get('performance_aircraft') for row in samples} != {expected_aircraft}:
            errors.append(f'{acid} did not resolve exactly to {expected_aircraft}')
        if any(row.get('performance_dummy', '').lower() == 'true' for row in samples):
            errors.append(f'{acid} used a dummy model')
        if any(row.get('performance_valid', '').lower() != 'true' for row in samples):
            errors.append(f'{acid} contains invalid performance samples')
        if any(int(row.get('performance_miss_count') or 0) != 0 for row in samples):
            errors.append(f'{acid} contains performance misses')
        if max(abs(_number(row, 'vertical_speed_m_s')) for row in samples) > 0.05:
            errors.append(f'{acid} did not remain in level flight')

        positive, negative, stable, mismatches = [], [], [], []
        for previous, current in zip(samples, samples[1:]):
            dt = _number(current, 'sim_time_s') - _number(previous, 'sim_time_s')
            if dt <= 0.0:
                errors.append(f'{acid} has a non-increasing timestamp')
                continue
            observed = (_number(current, 'tas_m_s') - _number(previous, 'tas_m_s')) / dt
            force = ((_number(current, 'thrust_n') - _number(current, 'drag_n')) /
                     _number(previous, 'mass_kg'))
            mismatch = abs(observed - force)
            if abs(observed) >= acceleration_threshold:
                mismatches.append(mismatch)
                (positive if observed > 0.0 else negative).append(current)
            else:
                stable.append(current)
            expected_mass = (_number(previous, 'mass_kg') -
                             _number(current, 'fuel_flow_kg_s') * dt)
            if abs(_number(current, 'mass_kg') - expected_mass) > 0.01:
                errors.append(f'{acid} mass/fuel integration mismatch at {current["sim_time_s"]}')
        if len(positive) < 3 or len(negative) < 3:
            errors.append(f'{acid} lacks acceleration/deceleration samples: '
                          f'{len(positive)}/{len(negative)}')
        maximum = max(mismatches, default=math.inf)
        if maximum > balance_tolerance:
            errors.append(f'{acid} maximum force-balance mismatch {maximum:.6f} m/s2 '
                          f'exceeds {balance_tolerance}')
        if positive and negative:
            mean = lambda group, field: sum(_number(row, field) for row in group) / len(group)
            if mean(positive, 'thrust_n') <= mean(negative, 'thrust_n'):
                errors.append(f'{acid} acceleration thrust is not above deceleration thrust')
            if mean(positive, 'fuel_flow_kg_s') < mean(negative, 'fuel_flow_kg_s'):
                errors.append(f'{acid} acceleration fuel is below deceleration fuel')
        if stable and max(abs((_number(row, 'thrust_n') - _number(row, 'drag_n')) /
                              _number(row, 'mass_kg')) for row in stable) > balance_tolerance:
            errors.append(f'{acid} stable samples do not return to thrust/drag equilibrium')
        summaries.append(f'{acid}: +ax={len(positive)}, -ax={len(negative)}, '
                         f'max mismatch={maximum:.6f} m/s2')

    # KINEMATIC and current level TEM propagation should remain identical; the
    # later joint-energy gate will deliberately test where their ownership differs.
    left = {row['sim_time_s']: row for row in by_acid.get(f'B{family}AK', [])}
    right = {row['sim_time_s']: row for row in by_acid.get(f'B{family}AT', [])}
    common = sorted(set(left).intersection(right), key=float)
    if len(common) < 25:
        errors.append('KINEMATIC/TEM aircraft lack aligned samples')
    elif max(abs(_number(left[t], 'tas_m_s') - _number(right[t], 'tas_m_s'))
             for t in common) > 1e-6:
        errors.append('KINEMATIC/TEM level TAS propagation differs')

    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(errors)
    return f'VALID: {len(rows)} BADA {family} acceleration rows; ' + '; '.join(summaries)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv')
    parser.add_argument('--family', choices=('3', '4'), required=True)
    args = parser.parse_args(argv)
    try:
        result = validate(args.csv, args.family)
    except (OSError, ValueError, KeyError, json.JSONDecodeError,
            ZeroDivisionError) as exc:
        result = f'INVALID evidence:\n  - evidence could not be validated: {exc}'
    print(result)
    return int(result.startswith('INVALID'))


if __name__ == '__main__':
    raise SystemExit(main())
