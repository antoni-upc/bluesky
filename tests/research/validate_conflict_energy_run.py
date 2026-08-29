#!/usr/bin/env python3
"""Validate licensed conflicting deceleration/climb BADA energy evidence."""

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


def _true(row, field):
    return row.get(field, '').lower() == 'true'


def _temperature_factor(row):
    isa_temperature = 288.15 - 0.0065 * _number(row, 'pressure_alt_m')
    return isa_temperature / _number(row, 'temperature_k')


def validate(path, family, power_tolerance=0.75, motion_tolerance=0.08):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    errors = []
    acid = f'B{family}CE'
    expected_aircraft = {'3': 'A320__', '4': 'A320-232'}[family]
    required = {'energy_share_factor', 'energy_allocation_policy',
                'requested_vertical_rate_m_s', 'applied_vertical_rate_m_s',
                'requested_acceleration_m_s2', 'applied_acceleration_m_s2',
                'idle_thrust_n', 'maximum_thrust_n'}
    if metadata.get('schema_version') != 'samples-v10':
        errors.append('metadata schema is not samples-v10')
    if metadata.get('scenario') != f'pybada-conflict-energy-bada{family}':
        errors.append('metadata scenario is incorrect')
    if metadata.get('sample_intervals_s') != [0.05]:
        errors.append(f'sample interval is not exactly 0.05 s: '
                      f'{metadata.get("sample_intervals_s")}')
    if metadata.get('event_total') not in (None, 0):
        errors.append('quality events were recorded')
    if not required.issubset(set(metadata.get('columns', ()))):
        errors.append('metadata lacks conflicting-energy fields')
    if len(rows) < 1000 or {row.get('acid') for row in rows} != {acid}:
        errors.append(f'expected at least 1000 rows for {acid}, got {len(rows)}')

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

    ordered = sorted(rows, key=lambda row: _number(row, 'sim_time_s'))
    for row in ordered:
        thrust = _number(row, 'thrust_n')
        lower, upper = sorted((_number(row, 'idle_thrust_n'),
                               _number(row, 'maximum_thrust_n')))
        if thrust < lower - 1e-6 or thrust > upper + 1e-6:
            errors.append('applied thrust is outside licensed idle/maximum bounds')
            break
        if _number(row, 'fuel_flow_kg_s') < 0.0 or _number(row, 'mass_kg') <= 0.0:
            errors.append('fuel flow or mass is non-physical')
            break
    conflict = [row for row in ordered
                if row.get('energy_allocation_policy') == 'BADA_ESF'
                and _number(row, 'requested_acceleration_m_s2') < -0.01
                and _number(row, 'requested_vertical_rate_m_s') > 0.1
                and _number(row, 'applied_acceleration_m_s2') > 1e-6
                and _number(row, 'applied_vertical_rate_m_s') > 0.1]
    recovery = [row for row in ordered
                if row.get('energy_allocation_policy') == 'HORIZONTAL_ADAPTED'
                and _number(row, 'requested_acceleration_m_s2') < -0.01
                and _number(row, 'applied_acceleration_m_s2') < -1e-6
                and abs(_number(row, 'applied_vertical_rate_m_s')) <= 1e-9]
    stable = [row for row in ordered
              if _number(row, 'sim_time_s') > 100.0
              and abs(_number(row, 'vertical_speed_m_s')) <= 1e-6
              and _true(row, 'speed_capture')]
    if len(conflict) < 100:
        errors.append(f'insufficient conflicting-command evidence: {len(conflict)}')
    if len(recovery) < 20:
        errors.append(f'insufficient post-climb deceleration evidence: {len(recovery)}')
    if len(stable) < 20:
        errors.append(f'insufficient stable dual-capture evidence: {len(stable)}')

    residuals, request_gaps, ax_errors, vs_errors, mass_errors = [], [], [], [], []
    conflict_ids = {id(row) for row in conflict}
    for previous, current in zip(ordered, ordered[1:]):
        dt = _number(current, 'sim_time_s') - _number(previous, 'sim_time_s')
        if dt <= 0.0:
            errors.append('timestamps are not strictly increasing')
            continue
        expected_mass = (_number(previous, 'mass_kg') -
                         _number(current, 'fuel_flow_kg_s') * dt)
        mass_errors.append(abs(_number(current, 'mass_kg') - expected_mass))
        if id(current) not in conflict_ids:
            continue
        tas = _number(current, 'tas_m_s')
        mass = _number(previous, 'mass_kg')
        available_power = ((_number(current, 'thrust_n') -
                            _number(current, 'drag_n')) * tas / mass)
        temperature_factor = _temperature_factor(current)
        requested_power = (
            tas * _number(current, 'requested_acceleration_m_s2') +
            G0 * _number(current, 'requested_vertical_rate_m_s') / temperature_factor)
        allocated_power = (
            tas * _number(current, 'applied_acceleration_m_s2') +
            G0 * _number(current, 'applied_vertical_rate_m_s') / temperature_factor)
        residuals.append(abs(available_power - allocated_power))
        request_gaps.append(abs(available_power - requested_power))
        if requested_power >= 0.0 or allocated_power <= 0.0:
            errors.append('commands do not demonstrate opposing requested/applied power')
        if abs(_number(current, 'thrust_n') -
               _number(current, 'maximum_thrust_n')) > 1e-6:
            errors.append('conflicting climb does not use maximum thrust')
        observed_ax = (_number(current, 'tas_m_s') -
                       _number(previous, 'tas_m_s')) / dt
        observed_vs = (_number(current, 'geometric_alt_m') -
                       _number(previous, 'geometric_alt_m')) / dt
        ax_errors.append(abs(observed_ax -
                             _number(current, 'applied_acceleration_m_s2')))
        vs_errors.append(abs(observed_vs -
                             _number(current, 'applied_vertical_rate_m_s')))
    if min(request_gaps, default=-math.inf) < 10.0:
        errors.append('requested power is not materially incompatible with available power')
    if max(residuals, default=math.inf) > power_tolerance:
        errors.append(f'maximum applied total-energy residual '
                      f'{max(residuals, default=math.inf):.6f} W/kg')
    if max(ax_errors, default=math.inf) > motion_tolerance:
        errors.append(f'maximum horizontal motion mismatch '
                      f'{max(ax_errors, default=math.inf):.6f} m/s2')
    if max(vs_errors, default=math.inf) > motion_tolerance:
        errors.append(f'maximum vertical motion mismatch '
                      f'{max(vs_errors, default=math.inf):.6f} m/s')
    if max(mass_errors, default=math.inf) > 0.01:
        errors.append(f'maximum mass/fuel mismatch '
                      f'{max(mass_errors, default=math.inf):.6f} kg')
    if conflict and not all(0.0 <= _number(row, 'energy_share_factor') <= 2.0
                            for row in conflict):
        errors.append('conflict samples contain implausible energy-share factors')

    final = ordered[-1] if ordered else None
    commanded_altitude = 12000.0 * 0.3048
    if final is not None:
        if abs(_number(final, 'geometric_alt_m') - commanded_altitude) > 0.02:
            errors.append(f'aircraft did not capture 12000 ft: '
                          f'{_number(final, "geometric_alt_m"):.6f} m')
        if abs(_number(final, 'vertical_speed_m_s')) > 1e-6:
            errors.append('final vertical speed is not zero')
        if not _true(final, 'speed_capture') or abs(
                _number(final, 'tas_m_s') - _number(final, 'target_tas_m_s')) > 0.02:
            errors.append('final selected speed is not captured')

    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(dict.fromkeys(errors))
    return (f'VALID: {len(rows)} BADA {family} conflict-energy rows; '
            f'conflict={len(conflict)}, recovery={len(recovery)}, stable={len(stable)}, '
            f'min request gap={min(request_gaps):.6f} W/kg, '
            f'max power residual={max(residuals):.6f} W/kg, '
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
