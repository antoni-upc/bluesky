#!/usr/bin/env python3
"""Validate licensed TEM thrust saturation and target capture evidence."""

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


def _true(row, field):
    return row.get(field, '').lower() == 'true'


def validate(path, family, balance_tolerance=0.04, state_tolerance=0.02):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    errors = []
    scenario = f'pybada-saturation-bada{family}'
    expected_model = f'PYBADATEM-BADA{family}'
    expected_aircraft = {'3': 'A320__', '4': 'A320-232'}[family]
    required_fields = {
        'required_thrust_n', 'idle_thrust_n', 'maximum_thrust_n',
        'target_tas_m_s', 'requested_acceleration_m_s2',
        'applied_acceleration_m_s2', 'thrust_limited',
        'thrust_limitation_reason', 'speed_capture'}
    if metadata.get('schema_version') not in ('samples-v8', 'samples-v9', 'samples-v10'):
        errors.append('metadata schema is not compatible samples-v8/v9/v10')
    if metadata.get('scenario') != scenario:
        errors.append(f'metadata scenario is not {scenario}')
    if not required_fields.issubset(set(metadata.get('columns', ()))):
        errors.append('metadata lacks one or more samples-v9 saturation fields')
    intervals = metadata.get('sample_intervals_s')
    if not isinstance(intervals, list) or intervals != [0.05]:
        errors.append(f'sample interval is not exactly 0.05 s: {intervals}')
    if metadata.get('event_total') not in (None, 0):
        errors.append(f'quality events were recorded: {metadata.get("event_total")}')

    by_acid = defaultdict(list)
    for row in rows:
        by_acid[row.get('acid')].append(row)
    expected = {f'B{family}ST': 'TEM'}
    if set(by_acid) != set(expected):
        errors.append(f'aircraft are {sorted(by_acid)}, expected {sorted(expected)}')

    summaries = []
    for acid, mode in expected.items():
        samples = sorted(by_acid.get(acid, ()), key=lambda row: _number(row, 'sim_time_s'))
        if len(samples) < 100:
            errors.append(f'{acid} has only {len(samples)} samples')
            continue
        if {row.get('dynamics_mode') for row in samples} != {mode}:
            errors.append(f'{acid} did not remain in {mode}')
        if {row.get('performance_model') for row in samples} != {expected_model}:
            errors.append(f'{acid} did not remain on {expected_model}')
        if {row.get('performance_aircraft') for row in samples} != {expected_aircraft}:
            errors.append(f'{acid} did not resolve exactly to {expected_aircraft}')
        if any(_true(row, 'performance_dummy') for row in samples):
            errors.append(f'{acid} used dummy data')
        if any(not _true(row, 'performance_valid') for row in samples):
            errors.append(f'{acid} contains invalid performance samples')
        if any(int(row.get('performance_miss_count') or 0) for row in samples):
            errors.append(f'{acid} contains performance misses')
        if max(abs(_number(row, 'vertical_speed_m_s')) for row in samples) > 0.05:
            errors.append(f'{acid} did not remain level')

        limited = [row for row in samples if _true(row, 'thrust_limited')]
        above = [row for row in limited
                 if row.get('thrust_limitation_reason') == 'ABOVE_MAXIMUM_THRUST']
        below = [row for row in limited
                 if row.get('thrust_limitation_reason') == 'BELOW_IDLE_THRUST']
        if len(above) < 3 or len(below) < 3:
            errors.append(f'{acid} lacks maximum/idle saturation: {len(above)}/{len(below)}')
        for row in above:
            if _number(row, 'required_thrust_n') <= _number(row, 'maximum_thrust_n'):
                errors.append(f'{acid} maximum-thrust reason contradicts required thrust')
                break
            if abs(_number(row, 'thrust_n') - _number(row, 'maximum_thrust_n')) > 0.01:
                errors.append(f'{acid} did not apply maximum thrust during saturation')
                break
        for row in below:
            if _number(row, 'required_thrust_n') >= _number(row, 'idle_thrust_n'):
                errors.append(f'{acid} idle-thrust reason contradicts required thrust')
                break
            if abs(_number(row, 'thrust_n') - _number(row, 'idle_thrust_n')) > 0.01:
                errors.append(f'{acid} did not apply idle thrust during saturation')
                break

        mismatches = []
        for previous, current in zip(samples, samples[1:]):
            dt = _number(current, 'sim_time_s') - _number(previous, 'sim_time_s')
            if dt <= 0.0:
                errors.append(f'{acid} has non-increasing timestamps')
                continue
            observed = (_number(current, 'tas_m_s') - _number(previous, 'tas_m_s')) / dt
            force = ((_number(current, 'thrust_n') - _number(current, 'drag_n')) /
                     _number(previous, 'mass_kg'))
            applied = _number(current, 'applied_acceleration_m_s2')
            requested = _number(current, 'requested_acceleration_m_s2')
            if _true(current, 'thrust_limited'):
                mismatches.extend((abs(observed - force), abs(observed - applied)))
                direction = math.copysign(1.0, _number(current, 'target_tas_m_s') -
                                          _number(previous, 'tas_m_s'))
                if direction * observed < -state_tolerance:
                    errors.append(f'{acid} moved away from its target during saturation')
                if direction * (_number(current, 'tas_m_s') -
                                _number(current, 'target_tas_m_s')) > state_tolerance:
                    errors.append(f'{acid} overshot its selected-speed target')
            expected_mass = (_number(previous, 'mass_kg') -
                             _number(current, 'fuel_flow_kg_s') * dt)
            if abs(_number(current, 'mass_kg') - expected_mass) > 0.01:
                errors.append(f'{acid} mass/fuel mismatch at {current["sim_time_s"]}')
        if max(mismatches, default=math.inf) > balance_tolerance:
            errors.append(f'{acid} maximum applied-motion mismatch is '
                          f'{max(mismatches, default=math.inf):.6f} m/s2')

        target_levels = {round(_number(row, 'target_tas_m_s'), 6) for row in samples}
        captures = [row for row in samples if _true(row, 'speed_capture')]
        commanded_captures = [row for row in captures
                              if abs(_number(row, 'target_tas_m_s') -
                                     _number(row, 'tas_m_s')) <= state_tolerance]
        captured_levels = {round(_number(row, 'target_tas_m_s'), 6)
                           for row in commanded_captures}
        if len(target_levels) < 2 or not target_levels.issubset(captured_levels):
            errors.append(f'{acid} did not record target capture and recovery')
        summaries.append(f'{acid}: max={len(above)}, idle={len(below)}, '
                         f'captures={len(commanded_captures)}')

    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(errors)
    return f'VALID: {len(rows)} BADA {family} saturation rows; ' + '; '.join(summaries)


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
