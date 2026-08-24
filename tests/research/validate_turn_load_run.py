#!/usr/bin/env python3
"""Validate licensed BADA coordinated-turn load and drag evidence."""

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


def validate(path, family):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    errors = []
    expected = {f'B{family}TS', f'B{family}TT'}
    expected_aircraft = {'3': 'A320__', '4': 'A320-232'}[family]
    required = {'propulsion_bank_angle_deg', 'propulsion_load_factor', 'drag_n',
                'thrust_n', 'fuel_flow_kg_s', 'mass_kg'}
    if metadata.get('schema_version') != 'samples-v10':
        errors.append('metadata schema is not samples-v10')
    if metadata.get('scenario') != f'pybada-turn-load-bada{family}':
        errors.append('metadata scenario does not match the requested family')
    if not required.issubset(set(metadata.get('columns', ()))):
        errors.append('metadata lacks turn-load propulsion fields')
    if metadata.get('sample_intervals_s') != [0.05]:
        errors.append(f'sample interval is not exactly 0.05 s: {metadata.get("sample_intervals_s")}')
    if metadata.get('event_total') not in (None, 0):
        errors.append(f'quality events were recorded: {metadata.get("event_total")}')

    by_acid = defaultdict(dict)
    for row in rows:
        by_acid[row.get('acid')][_number(row, 'sim_time_s')] = row
    if set(by_acid) != expected:
        errors.append(f'aircraft are {sorted(by_acid)}, expected {sorted(expected)}')
        return 'INVALID evidence:\n  - ' + '\n  - '.join(errors)
    straight = by_acid[f'B{family}TS']
    turning = by_acid[f'B{family}TT']
    aligned = sorted(set(straight).intersection(turning))
    loaded = []
    drag_gains = []
    fuel_gains = []
    for timestamp in aligned:
        control, turn = straight[timestamp], turning[timestamp]
        for row in (control, turn):
            if row.get('performance_model') != f'PYBADATEM-BADA{family}':
                errors.append('performance model provenance changed')
            if row.get('performance_aircraft') != expected_aircraft:
                errors.append('aircraft model did not resolve exactly')
            if _true(row, 'performance_dummy') or not _true(row, 'performance_valid'):
                errors.append('dummy or invalid performance sample found')
            if int(row.get('performance_miss_count') or 0):
                errors.append('performance miss found')
            if row.get('dynamics_mode') != 'TEM':
                errors.append('aircraft did not remain in TEM')
            if abs(_number(row, 'vertical_speed_m_s')) > 0.02:
                errors.append('aircraft did not remain level')
            force = (_number(row, 'thrust_n') - _number(row, 'drag_n')) / _number(row, 'mass_kg')
            if abs(force - _number(row, 'applied_acceleration_m_s2')) > 1e-6:
                errors.append('force balance does not use recorded loaded drag')
        if (_number(control, 'propulsion_bank_angle_deg') != 0.0 or
                _number(control, 'propulsion_load_factor') != 1.0):
            errors.append('straight control did not retain exact unit load')
        bank = abs(_number(turn, 'propulsion_bank_angle_deg'))
        load = _number(turn, 'propulsion_load_factor')
        if bank > 1.0:
            loaded.append(turn)
            if abs(load - 1.0 / math.cos(math.radians(bank))) > 1e-9:
                errors.append('turn load is inconsistent with recorded bank')
            drag_gains.append(_number(turn, 'drag_n') - _number(control, 'drag_n'))
            fuel_gains.append(_number(turn, 'fuel_flow_kg_s') - _number(control, 'fuel_flow_kg_s'))
    if len(rows) < 400 or len(loaded) < 20:
        errors.append(f'insufficient fixed-step evidence: rows={len(rows)}, loaded={len(loaded)}')
    if not drag_gains or min(drag_gains) <= 0.0:
        errors.append('turning drag did not remain above the aligned straight control')
    if not fuel_gains or min(fuel_gains) <= 0.0:
        errors.append('turning fuel flow did not remain above the aligned straight control')
    headings = [_number(row, 'heading_deg') for row in loaded]
    if not headings or max(abs(value - 90.0) for value in headings) < 1.0:
        errors.append('turning aircraft heading did not change materially')
    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(dict.fromkeys(errors))
    return (f'VALID: {len(rows)} BADA {family} turn-load rows; loaded={len(loaded)}, '
            f'min drag gain={min(drag_gains):.3f} N, min fuel gain={min(fuel_gains):.6f} kg/s')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv')
    parser.add_argument('--family', choices=('3', '4'), required=True)
    args = parser.parse_args(argv)
    try:
        result = validate(args.csv, args.family)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, ZeroDivisionError) as exc:
        result = f'INVALID evidence:\n  - evidence could not be validated: {exc}'
    print(result)
    return int(result.startswith('INVALID'))


if __name__ == '__main__':
    raise SystemExit(main())
