#!/usr/bin/env python3
"""Validate licensed turn-loaded horizontal/vertical total-energy evidence."""

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


def _angle_error(value, target):
    return abs((value - target + 180.0) % 360.0 - 180.0)


def validate(path, family, power_tolerance=0.75, motion_tolerance=0.08):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    errors = []
    acid = f'B{family}TE'
    expected_aircraft = {'3': 'A320__', '4': 'A320-232'}[family]
    required = {'propulsion_bank_angle_deg', 'propulsion_load_factor',
                'energy_share_factor', 'energy_allocation_policy',
                'requested_vertical_rate_m_s', 'applied_vertical_rate_m_s',
                'requested_acceleration_m_s2', 'applied_acceleration_m_s2'}
    if metadata.get('schema_version') != 'samples-v10':
        errors.append('metadata schema is not samples-v10')
    if metadata.get('scenario') != f'pybada-turn-energy-bada{family}':
        errors.append('metadata scenario is incorrect')
    if metadata.get('sample_intervals_s') != [0.05]:
        errors.append(f'sample interval is not exactly 0.05 s: '
                      f'{metadata.get("sample_intervals_s")}')
    if metadata.get('event_total') not in (None, 0):
        errors.append('quality events were recorded')
    if not required.issubset(set(metadata.get('columns', ()))):
        errors.append('metadata lacks turn-energy fields')
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
    loaded = [row for row in ordered
              if abs(_number(row, 'propulsion_bank_angle_deg')) > 1.0]
    loaded_joint = [row for row in loaded
                    if row.get('energy_allocation_policy') == 'BADA_ESF'
                    and abs(_number(row, 'applied_vertical_rate_m_s')) > 0.1]
    recovered = [row for row in ordered
                 if _number(row, 'sim_time_s') > 70.0
                 and _number(row, 'propulsion_bank_angle_deg') == 0.0
                 and _number(row, 'propulsion_load_factor') == 1.0]
    if len(loaded) < 100 or len(loaded_joint) < 100:
        errors.append(f'insufficient loaded joint evidence: loaded={len(loaded)}, '
                      f'joint={len(loaded_joint)}')
    if len(recovered) < 20:
        errors.append('insufficient straight-flight recovery after heading capture')
    for row in loaded:
        bank = abs(_number(row, 'propulsion_bank_angle_deg'))
        load = _number(row, 'propulsion_load_factor')
        if abs(load - 1.0 / math.cos(math.radians(bank))) > 1e-9:
            errors.append('propulsion load is inconsistent with bank angle')
            break

    residuals, ax_errors, vs_errors, mass_errors = [], [], [], []
    joint_ids = {id(row) for row in loaded_joint}
    for previous, current in zip(ordered, ordered[1:]):
        dt = _number(current, 'sim_time_s') - _number(previous, 'sim_time_s')
        if dt <= 0.0:
            errors.append('timestamps are not strictly increasing')
            continue
        expected_mass = (_number(previous, 'mass_kg') -
                         _number(current, 'fuel_flow_kg_s') * dt)
        mass_errors.append(abs(_number(current, 'mass_kg') - expected_mass))
        if id(current) not in joint_ids:
            continue
        tas = _number(current, 'tas_m_s')
        mass = _number(previous, 'mass_kg')
        loaded_specific_power = ((_number(current, 'thrust_n') -
                                  _number(current, 'drag_n')) * tas / mass)
        isa_temperature = 288.15 - 0.0065 * _number(current, 'pressure_alt_m')
        temperature_factor = isa_temperature / _number(current, 'temperature_k')
        allocated_specific_power = (
            tas * _number(current, 'applied_acceleration_m_s2') +
            G0 * _number(current, 'applied_vertical_rate_m_s') / temperature_factor)
        residuals.append(abs(loaded_specific_power - allocated_specific_power))
        observed_ax = (_number(current, 'tas_m_s') -
                       _number(previous, 'tas_m_s')) / dt
        observed_vs = (_number(current, 'geometric_alt_m') -
                       _number(previous, 'geometric_alt_m')) / dt
        ax_errors.append(abs(observed_ax -
                             _number(current, 'applied_acceleration_m_s2')))
        vs_errors.append(abs(observed_vs -
                             _number(current, 'applied_vertical_rate_m_s')))
    if max(residuals, default=math.inf) > power_tolerance:
        errors.append(f'maximum loaded total-energy residual '
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
    if loaded_joint and not all(0.0 <= _number(row, 'energy_share_factor') <= 2.0
                                for row in loaded_joint):
        errors.append('loaded joint samples contain implausible energy-share factors')

    commanded_altitude = 12000.0 * 0.3048
    final = ordered[-1] if ordered else None
    if final is not None:
        if abs(_number(final, 'geometric_alt_m') - commanded_altitude) > 0.02:
            errors.append(f'aircraft did not capture 12000 ft: '
                          f'{_number(final, "geometric_alt_m"):.6f} m')
        if abs(_number(final, 'vertical_speed_m_s')) > 1e-6:
            errors.append('final vertical speed is not zero')
        if not _true(final, 'speed_capture') or abs(
                _number(final, 'tas_m_s') - _number(final, 'target_tas_m_s')) > 0.02:
            errors.append('final selected speed is not captured')
        if _angle_error(_number(final, 'heading_deg'), 270.0) > 0.02:
            errors.append(f'final heading is not captured: {final.get("heading_deg")}')
        if (_number(final, 'propulsion_bank_angle_deg') != 0.0 or
                _number(final, 'propulsion_load_factor') != 1.0):
            errors.append('final propulsion state did not return to straight flight')

    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(dict.fromkeys(errors))
    return (f'VALID: {len(rows)} BADA {family} turn-energy rows; '
            f'loaded joint={len(loaded_joint)}, recovery={len(recovered)}, '
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
