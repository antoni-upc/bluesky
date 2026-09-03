#!/usr/bin/env python3
"""Validate licensed BADA 4 approach lateral-envelope evidence."""

import argparse
import csv
import json
from pathlib import Path


def _load(path):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    events = [json.loads(line) for line in
              path.with_suffix('.events.jsonl').read_text(encoding='utf-8').splitlines()]
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    return rows, events, metadata


def _check_approach_bounds(row, acid, errors):
    if row.get('envelope_lateral_configuration') != 'AP':
        errors.append(f'{acid} final lateral configuration is not AP')
    try:
        minimum = float(row['minimum_load_factor'])
        maximum = float(row['maximum_load_factor'])
        bank_max = float(row['maximum_bank_angle_deg'])
    except (KeyError, TypeError, ValueError):
        errors.append(f'{acid} lacks numeric approach lateral evidence')
        return None
    if abs(minimum) > 1e-6 or abs(maximum - 2.0) > 1e-6:
        errors.append(f'{acid} did not select nf3/nf1 load bounds 0.0..2.0')
    if abs(bank_max - 60.0) > 0.05:
        errors.append(f'{acid} approach bank maximum is not near 60 degrees')
    return maximum, bank_max


def _check_effective_metadata(metadata, acids, errors):
    effective = {item.get('aircraft'): item
                 for item in metadata.get('effective_envelope', [])}
    for acid in acids:
        item = effective.get(acid)
        if item is None:
            errors.append(f'{acid} has no final effective-envelope metadata')
            continue
        expected = {
            'configuration_mode': 'PYBADA', 'configuration': 'AP',
            'high_lift_id': 3.0, 'landing_gear': 'LGUP',
            'minimum_limit_name': 'nf3', 'maximum_limit_name': 'nf1'}
        for field, value in expected.items():
            if item.get(field) != value:
                errors.append(f'{acid} metadata {field} is not {value}')
        try:
            minimum = float(item['minimum_load_factor'])
            maximum = float(item['maximum_load_factor'])
            bank_max = float(item['maximum_bank_angle_deg'])
        except (KeyError, TypeError, ValueError):
            errors.append(f'{acid} metadata lacks numeric AP lateral bounds')
        else:
            if (abs(minimum) > 1e-6 or abs(maximum - 2.0) > 1e-6 or
                    abs(bank_max - 60.0) > 0.05):
                errors.append(f'{acid} metadata does not retain AP nf3/nf1 bounds')


def validate(path, abort=False):
    rows, events, metadata = _load(path)
    errors = []
    if metadata.get('schema_version') not in ('samples-v7', 'samples-v8', 'samples-v9', 'samples-v10'):
        errors.append('metadata schema is not compatible samples-v7/v8/v9/v10')
    expected_scenario = ('pybada-envelope-approach-abort' if abort
                         else 'pybada-envelope-approach')
    if metadata.get('scenario') != expected_scenario:
        errors.append(f'metadata scenario is not {expected_scenario}')
    latest = {row.get('acid'): row for row in rows}

    if abort:
        _check_effective_metadata(metadata, ('APAB',), errors)
        row = latest.get('APAB')
        if row is None:
            errors.append('APAB has no finalized sample')
        else:
            _check_approach_bounds(row, 'APAB', errors)
        matching = [event for event in events if event.get('aircraft') == 'APAB'
                    and event.get('action') == 'ABORTED'
                    and event.get('reason') == 'BANK_ANGLE,LOAD_FACTOR']
        if len(matching) != 1:
            errors.append(f'APAB expected one ABORTED bank/load event, found {len(matching)}')
        else:
            requested = matching[0].get('requested') or {}
            applied = matching[0].get('applied') or {}
            try:
                if abs(float(requested['bank_angle_deg']) - 70.0) > 0.05:
                    errors.append('APAB requested bank is not near 70 degrees')
                if abs(float(requested['load_factor']) - 2.9238044) > 0.005:
                    errors.append('APAB requested load is not near 2.924')
                if requested != applied:
                    errors.append('APAB ABORT evidence does not retain requested state')
            except (KeyError, TypeError, ValueError):
                errors.append('APAB event lacks numeric requested/applied bank and load')
        if metadata.get('quality_status') != 'ABORTED':
            errors.append('metadata quality status is not ABORTED')
        if metadata.get('event_total') != 1:
            errors.append(f'metadata event total is not 1: {metadata.get("event_total")}')
        if errors:
            return 'INVALID evidence:\n  - ' + '\n  - '.join(errors)
        return 'VALID: approach AP ABORT evidence finalized before HOLD'

    expected = {'APPR': ('REPORT', 'INFEASIBLE', 'ACCEPTED'),
                'APPE': ('ENFORCE', 'VALID', 'LIMITED')}
    _check_effective_metadata(metadata, expected, errors)
    for acid, (policy, status, action) in expected.items():
        row = latest.get(acid)
        if row is None:
            errors.append(f'{acid} has no final sample')
            continue
        bounds = _check_approach_bounds(row, acid, errors)
        if bounds is None:
            continue
        maximum, bank_max = bounds
        try:
            bank = float(row['bank_angle_deg'])
            load = float(row['load_factor'])
        except (KeyError, TypeError, ValueError):
            errors.append(f'{acid} lacks numeric applied bank/load evidence')
            continue
        if (row.get('envelope_policy'), row.get('envelope_status')) != (policy, status):
            errors.append(f'{acid} final policy/status is not {policy}/{status}')
        matching = [event for event in events if event.get('aircraft') == acid
                    and event.get('reason') == 'BANK_ANGLE,LOAD_FACTOR']
        if len(matching) != 1:
            errors.append(f'{acid} expected one bank/load event, found {len(matching)}')
        elif matching[0].get('action') != action:
            errors.append(f'{acid} event action is not {action}')
        if policy == 'REPORT' and not (bank > bank_max + 0.01 and load > maximum + 0.001):
            errors.append('APPR did not retain the excessive bank/load request')
        if policy == 'ENFORCE' and not (bank <= bank_max + 0.01 and
                                        load <= maximum + 0.001):
            errors.append('APPE exceeded its enforced approach bank/load maximum')
    if metadata.get('event_total') != 2:
        errors.append(f'metadata event total is not 2: {metadata.get("event_total")}')
    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(errors)
    return (f'VALID: {len(rows)} samples, approach AP REPORT and ENFORCE '
            'are isolated and transition-only')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('csv')
    parser.add_argument('--abort', action='store_true')
    args = parser.parse_args()
    try:
        result = validate(args.csv, args.abort)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        result = f'INVALID evidence:\n  - evidence could not be validated: {exc}'
    print(result)
    if result.startswith('INVALID'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
