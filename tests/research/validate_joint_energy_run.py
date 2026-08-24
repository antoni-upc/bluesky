#!/usr/bin/env python3
"""Validate licensed simultaneous horizontal/vertical BADA energy allocation."""

import argparse
import csv
import json
import math
from pathlib import Path


G0 = 9.80665


def _number(row, field):
    value = float(row[field])
    if not math.isfinite(value):
        raise ValueError(f'{row.get("acid", "?")} {field} is non-finite')
    return value


def validate(path, family, power_tolerance=0.75, motion_tolerance=0.08):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    errors = []
    acid = f'B{family}JE'
    expected_aircraft = {'3': 'A320__', '4': 'A320-232'}[family]
    if metadata.get('schema_version') not in ('samples-v9', 'samples-v10'):
        errors.append('metadata schema is not compatible samples-v9/v10')
    if metadata.get('scenario') != f'pybada-joint-energy-bada{family}':
        errors.append('metadata scenario is incorrect')
    if metadata.get('sample_intervals_s') != [0.05]:
        errors.append(f'sample interval is not exactly 0.05 s: '
                      f'{metadata.get("sample_intervals_s")}')
    if metadata.get('event_total') not in (None, 0):
        errors.append('quality events were recorded')
    if len(rows) < 500 or {row.get('acid') for row in rows} != {acid}:
        errors.append(f'expected at least 500 rows for {acid}, got {len(rows)}')
    if rows:
        checks = {
            'performance_model': f'PYBADATEM-BADA{family}',
            'performance_dataset_version': {'3': '3.15', '4': '4.2'}[family],
            'performance_aircraft': expected_aircraft,
            'performance_dummy': 'False', 'performance_valid': 'True',
            'performance_miss_count': '0', 'dynamics_mode': 'TEM'}
        for field, expected in checks.items():
            values = {row.get(field) for row in rows}
            if values != {expected}:
                errors.append(f'{field} values are {sorted(values)}, expected {expected}')

    joint = [row for row in rows
             if row.get('energy_allocation_policy') == 'BADA_ESF'
             and abs(_number(row, 'applied_vertical_rate_m_s')) > 0.1]
    if len(joint) < 20:
        errors.append(f'only {len(joint)} joint BADA_ESF samples')
    residuals, ax_errors, vs_errors = [], [], []
    ordered = sorted(rows, key=lambda row: _number(row, 'sim_time_s'))
    for previous, current in zip(ordered, ordered[1:]):
        if current not in joint:
            continue
        dt = _number(current, 'sim_time_s') - _number(previous, 'sim_time_s')
        if dt <= 0.0:
            errors.append('timestamps are not strictly increasing')
            continue
        tas = _number(current, 'tas_m_s')
        mass = _number(previous, 'mass_kg')
        excess_specific_power = ((_number(current, 'thrust_n') -
                                  _number(current, 'drag_n')) * tas / mass)
        isa_temperature = 288.15 - 0.0065 * _number(current, 'pressure_alt_m')
        temperature_factor = isa_temperature / _number(current, 'temperature_k')
        allocated_specific_power = (
            tas * _number(current, 'applied_acceleration_m_s2') +
            G0 * _number(current, 'applied_vertical_rate_m_s') / temperature_factor)
        residuals.append(abs(excess_specific_power - allocated_specific_power))
        observed_ax = (_number(current, 'tas_m_s') -
                       _number(previous, 'tas_m_s')) / dt
        observed_vs = (_number(current, 'geometric_alt_m') -
                       _number(previous, 'geometric_alt_m')) / dt
        ax_errors.append(abs(observed_ax -
                             _number(current, 'applied_acceleration_m_s2')))
        vs_errors.append(abs(observed_vs -
                             _number(current, 'applied_vertical_rate_m_s')))
    if max(residuals, default=math.inf) > power_tolerance:
        errors.append(f'maximum total-energy residual '
                      f'{max(residuals, default=math.inf):.6f} W/kg')
    if max(ax_errors, default=math.inf) > motion_tolerance:
        errors.append(f'maximum horizontal motion mismatch '
                      f'{max(ax_errors, default=math.inf):.6f} m/s2')
    if max(vs_errors, default=math.inf) > motion_tolerance:
        errors.append(f'maximum vertical motion mismatch '
                      f'{max(vs_errors, default=math.inf):.6f} m/s')
    if joint and not all(0.0 <= _number(row, 'energy_share_factor') <= 2.0
                         for row in joint):
        errors.append('joint samples contain implausible energy-share factors')
    commanded_altitude = 12000.0 * 0.3048
    if rows and abs(_number(rows[-1], 'geometric_alt_m') - commanded_altitude) > 0.02:
        errors.append(f'aircraft did not exactly capture 12000 ft: '
                      f'{_number(rows[-1], "geometric_alt_m"):.6f} m')
    if rows and (abs(_number(rows[-1], 'vertical_speed_m_s')) > 1e-6 or
                 rows[-1].get('speed_capture', '').lower() != 'true'):
        errors.append('final speed/vertical state is not captured and stable')
    if any(_number(current, 'mass_kg') > _number(previous, 'mass_kg') + 1e-9
           for previous, current in zip(ordered, ordered[1:])):
        errors.append('mass increased')

    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(errors)
    return (f'VALID: {len(rows)} BADA {family} joint-energy rows; '
            f'joint={len(joint)}, max power residual={max(residuals):.6f} W/kg, '
            f'max ax error={max(ax_errors):.6f} m/s2, '
            f'max vs error={max(vs_errors):.6f} m/s')


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
