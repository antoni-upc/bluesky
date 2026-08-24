#!/usr/bin/env python3
"""Validate the licensed large-route REPORT/OFF comparison."""

import csv
import json
import math
from collections import defaultdict
from pathlib import Path
import sys


ACIDS = {'ERNV', 'OFNV', 'ERNO', 'OFNO'}
PAIRS = (('ERNV', 'OFNV'), ('ERNO', 'OFNO'))


def _angle_difference(a, b):
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _number(row, field):
    return float(row[field])


def validate(path):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    events = [json.loads(line) for line in
              path.with_suffix('.events.jsonl').read_text(encoding='utf-8').splitlines()]
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    errors = []
    if metadata.get('schema_version') != 'samples-v9':
        errors.append('metadata schema is not samples-v9')
    if metadata.get('scenario') != 'pybada-route-speed-gui':
        errors.append('metadata scenario is not pybada-route-speed-gui')
    if metadata.get('event_total') != len(events):
        errors.append('metadata event total does not match the JSONL stream')
    if metadata.get('sample_intervals_s') != [1.0]:
        errors.append(f'sample intervals are not exactly [1.0]: {metadata.get("sample_intervals_s")}')

    by_acid = defaultdict(list)
    for row in rows:
        by_acid[row.get('acid')].append(row)
    if set(by_acid) != ACIDS:
        errors.append(f'sampled aircraft are {sorted(by_acid)}, expected {sorted(ACIDS)}')
    for acid in ACIDS:
        by_acid[acid].sort(key=lambda row: _number(row, 'sim_time_s'))
        samples = by_acid[acid]
        if len(samples) < 740:
            errors.append(f'{acid} has only {len(samples)} samples; full route was not recorded')
        elif _number(samples[-1], 'sim_time_s') - _number(samples[0], 'sim_time_s') < 740.0:
            errors.append(f'{acid} recorded duration is shorter than 740 seconds')

    effective = {item.get('aircraft'): item
                 for item in metadata.get('effective_envelope', [])}
    expected_policy = {'ERNV': 'REPORT', 'ERNO': 'REPORT',
                       'OFNV': 'OFF', 'OFNO': 'OFF'}
    for acid, policy in expected_policy.items():
        item = effective.get(acid)
        if item is None:
            errors.append(f'{acid} lacks final effective-envelope metadata')
            continue
        if item.get('policy') != policy:
            errors.append(f'{acid} metadata policy is not {policy}')
        if item.get('configuration_mode') != 'PYBADA':
            errors.append(f'{acid} metadata configuration mode is not PYBADA')
        policies = {row.get('envelope_policy') for row in by_acid[acid]}
        if policies != {policy}:
            errors.append(f'{acid} sampled policies are {sorted(policies)}, expected {policy}')

    # Navigation-off aircraft must retain level eastbound flight even though
    # valid routes and constraints were loaded for them.
    for acid, initial_lat in (('ERNO', 41.240), ('OFNO', 41.280)):
        samples = by_acid[acid]
        if not samples:
            continue
        if max(abs(_number(row, 'lat_deg') - initial_lat) for row in samples) > 0.002:
            errors.append(f'{acid} did not remain on its navigation-off latitude')
        if max(_angle_difference(_number(row, 'track_deg'), 90.0) for row in samples) > 1.0:
            errors.append(f'{acid} did not retain an eastbound navigation-off track')
        if max(abs(_number(row, 'geometric_alt_m') - 914.4) for row in samples) > 1.0:
            errors.append(f'{acid} did not retain 3,000 ft with navigation off')
        if max(abs(_number(row, 'cas_m_s') - 92.59992) for row in samples) > 0.1:
            errors.append(f'{acid} did not retain 180 kt CAS with navigation off')

    # Navigation-on aircraft must traverse the square, including all four
    # cardinal track sectors, and capture the final 3,000-ft/170-kt constraint.
    for acid in ('ERNV', 'OFNV'):
        samples = by_acid[acid]
        if not samples:
            continue
        tracks = [_number(row, 'track_deg') for row in samples]
        sectors = {
            'east': any(_angle_difference(value, 90.0) < 20.0 for value in tracks),
            'north': any(_angle_difference(value, 0.0) < 20.0 for value in tracks),
            'west': any(_angle_difference(value, 270.0) < 20.0 for value in tracks),
            'south': any(_angle_difference(value, 180.0) < 20.0 for value in tracks),
        }
        missing = [name for name, present in sectors.items() if not present]
        if missing:
            errors.append(f'{acid} did not capture route sectors: {",".join(missing)}')
        if max(_number(row, 'lon_deg') for row in samples) < 2.23:
            errors.append(f'{acid} did not reach the east side of its route')
        if max(_number(row, 'geometric_alt_m') for row in samples) < 1750.0:
            errors.append(f'{acid} did not capture the 6,000-ft route climb')
        final = samples[-20:]
        mean_alt = sum(_number(row, 'geometric_alt_m') for row in final) / len(final)
        mean_cas = sum(_number(row, 'cas_m_s') for row in final) / len(final)
        if abs(mean_alt - 914.4) > 75.0:
            errors.append(f'{acid} final altitude did not capture 3,000 ft: {mean_alt:.1f} m')
        if abs(mean_cas - 87.45548) > 3.0:
            errors.append(f'{acid} final speed did not capture 170 kt: {mean_cas:.2f} m/s')

    # Compare time-aligned REPORT and OFF states after removing the deliberate
    # 0.040-degree northward route offset.
    maxima = {}
    for report, off in PAIRS:
        report_time = {row['sim_time_s']: row for row in by_acid[report]}
        off_time = {row['sim_time_s']: row for row in by_acid[off]}
        common = sorted(set(report_time).intersection(off_time), key=float)
        if len(common) < 740:
            errors.append(f'{report}/{off} have only {len(common)} aligned samples')
            continue
        differences = defaultdict(list)
        for timestamp in common:
            left, right = report_time[timestamp], off_time[timestamp]
            differences['latitude_deg'].append(abs(
                _number(left, 'lat_deg') - (_number(right, 'lat_deg') - 0.040)))
            differences['longitude_deg'].append(abs(
                _number(left, 'lon_deg') - _number(right, 'lon_deg')))
            differences['altitude_m'].append(abs(
                _number(left, 'geometric_alt_m') - _number(right, 'geometric_alt_m')))
            differences['cas_m_s'].append(abs(
                _number(left, 'cas_m_s') - _number(right, 'cas_m_s')))
            differences['track_deg'].append(_angle_difference(
                _number(left, 'track_deg'), _number(right, 'track_deg')))
        pair_max = {key: max(values) for key, values in differences.items()}
        maxima[f'{report}/{off}'] = pair_max
        limits = {'latitude_deg': 0.001, 'longitude_deg': 0.001,
                  'altitude_m': 2.0, 'cas_m_s': 0.5, 'track_deg': 1.0}
        for field, limit in limits.items():
            if pair_max[field] > limit:
                errors.append(
                    f'{report}/{off} REPORT/OFF {field} difference '
                    f'{pair_max[field]:.6g} exceeds {limit}')

    event_acids = {event.get('aircraft') for event in events}
    if not events:
        errors.append('REPORT aircraft emitted no objective quality evidence')
    if not event_acids.issubset({'ERNV', 'ERNO'}):
        errors.append(f'OFF aircraft emitted events: {sorted(event_acids - {"ERNV", "ERNO"})}')
    if not {'ERNV', 'ERNO'}.issubset(event_acids):
        errors.append('one or more REPORT aircraft emitted no quality event')
    for acid in ('OFNV', 'OFNO'):
        if any(row.get('envelope_event_count') not in ('', '0') for row in by_acid[acid]):
            errors.append(f'{acid} OFF samples contain a nonzero event count')

    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(errors)
    metrics = '; '.join(
        f'{pair}: lat={values["latitude_deg"]:.6f} deg, '
        f'lon={values["longitude_deg"]:.6f} deg, alt={values["altitude_m"]:.2f} m, '
        f'CAS={values["cas_m_s"]:.3f} m/s, track={values["track_deg"]:.3f} deg'
        for pair, values in maxima.items())
    return (f'VALID: {len(rows)} samples, route capture and final constraints pass; '
            f'navigation-off is stable; REPORT/OFF propagation is equivalent; {metrics}')


def main():
    try:
        result = validate(sys.argv[1])
    except (IndexError, OSError, ValueError, KeyError, json.JSONDecodeError,
            ZeroDivisionError) as exc:
        result = f'INVALID evidence:\n  - evidence could not be validated: {exc}'
    print(result)
    if result.startswith('INVALID'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
