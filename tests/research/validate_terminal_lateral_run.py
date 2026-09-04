#!/usr/bin/env python3
"""Validate licensed BADA 4 TO/LD lateral-envelope evidence."""

import argparse
import csv
import json
from pathlib import Path


CONFIG = {
    'TO': {'hlid': 3.0, 'gear': 'LGUP'},
    'LD': {'hlid': 5.0, 'gear': 'LGDN'},
}


def _load(path):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    events = [json.loads(line) for line in
              path.with_suffix('.events.jsonl').read_text(encoding='utf-8').splitlines()]
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    return rows, events, metadata


def _check_bounds(values, acid, configuration, errors):
    if values.get('configuration') != configuration:
        errors.append(f'{acid} configuration is not {configuration}')
    expected = CONFIG[configuration]
    if values.get('high_lift_id') != expected['hlid']:
        errors.append(f'{acid} HLid is not {expected["hlid"]}')
    if values.get('landing_gear') != expected['gear']:
        errors.append(f'{acid} gear is not {expected["gear"]}')
    if values.get('minimum_limit_name') != 'nf3' or values.get('maximum_limit_name') != 'nf1':
        errors.append(f'{acid} DLM names are not nf3/nf1')
    try:
        minimum = float(values['minimum_load_factor'])
        maximum = float(values['maximum_load_factor'])
        bank_max = float(values['maximum_bank_angle_deg'])
    except (KeyError, TypeError, ValueError):
        errors.append(f'{acid} lacks numeric lateral bounds')
        return None
    if (abs(minimum) > 1e-6 or abs(maximum - 2.0) > 1e-6 or
            abs(bank_max - 60.0) > 0.05):
        errors.append(f'{acid} does not retain nf3/nf1 0.0..2.0 and 60-degree bounds')
    return maximum, bank_max


def _check_metadata(metadata, expected, errors):
    effective = {item.get('aircraft'): item
                 for item in metadata.get('effective_envelope', [])}
    for acid, configuration in expected.items():
        item = effective.get(acid)
        if item is None:
            errors.append(f'{acid} has no final effective-envelope metadata')
            continue
        if item.get('configuration_mode') != 'PYBADA':
            errors.append(f'{acid} configuration mode is not PYBADA')
        _check_bounds(item, acid, configuration, errors)


def validate(path, abort=None):
    rows, events, metadata = _load(path)
    errors = []
    if metadata.get('schema_version') not in ('samples-v7', 'samples-v8', 'samples-v9', 'samples-v10'):
        errors.append('metadata schema is not compatible samples-v7/v8/v9/v10')
    latest = {row.get('acid'): row for row in rows}

    if abort:
        acid = 'TOA' if abort == 'TO' else 'LDA'
        scenario = ('pybada-envelope-takeoff-abort' if abort == 'TO'
                    else 'pybada-envelope-landing-abort')
        if metadata.get('scenario') != scenario:
            errors.append(f'metadata scenario is not {scenario}')
        _check_metadata(metadata, {acid: abort}, errors)
        row = latest.get(acid)
        if row is None:
            errors.append(f'{acid} has no finalized sample')
        else:
            values = dict(row)
            values['configuration'] = row.get('envelope_lateral_configuration')
            values['high_lift_id'] = CONFIG[abort]['hlid']
            values['landing_gear'] = CONFIG[abort]['gear']
            values['minimum_limit_name'] = 'nf3'
            values['maximum_limit_name'] = 'nf1'
            _check_bounds(values, acid, abort, errors)
        matching = [event for event in events if event.get('aircraft') == acid
                    and event.get('action') == 'ABORTED'
                    and event.get('reason') == 'BANK_ANGLE,LOAD_FACTOR']
        if len(matching) != 1:
            errors.append(f'{acid} expected one ABORTED bank/load event, found {len(matching)}')
        else:
            requested = matching[0].get('requested') or {}
            applied = matching[0].get('applied') or {}
            try:
                if abs(float(requested['bank_angle_deg']) - 70.0) > 0.05:
                    errors.append(f'{acid} requested bank is not near 70 degrees')
                if abs(float(requested['load_factor']) - 2.9238044) > 0.005:
                    errors.append(f'{acid} requested load is not near 2.924')
                if requested != applied:
                    errors.append(f'{acid} ABORT evidence does not retain requested state')
            except (KeyError, TypeError, ValueError):
                errors.append(f'{acid} event lacks numeric requested/applied bank and load')
        if metadata.get('quality_status') != 'ABORTED':
            errors.append('metadata quality status is not ABORTED')
        if metadata.get('event_total') != 1:
            errors.append(f'metadata event total is not 1: {metadata.get("event_total")}')
        if errors:
            return 'INVALID evidence:\n  - ' + '\n  - '.join(errors)
        return f'VALID: {abort} ABORT evidence finalized before HOLD'

    if metadata.get('scenario') != 'pybada-envelope-terminal':
        errors.append('metadata scenario is not pybada-envelope-terminal')
    expected = {
        'TOR': ('TO', 'REPORT', 'INFEASIBLE', 'ACCEPTED'),
        'TOE': ('TO', 'ENFORCE', 'VALID', 'LIMITED'),
        'LDR': ('LD', 'REPORT', 'INFEASIBLE', 'ACCEPTED'),
        'LDE': ('LD', 'ENFORCE', 'VALID', 'LIMITED'),
    }
    _check_metadata(metadata, {acid: values[0] for acid, values in expected.items()}, errors)
    for acid, (configuration, policy, status, action) in expected.items():
        history = {row.get('envelope_lateral_configuration')
                   for row in rows if row.get('acid') == acid}
        if history != {configuration}:
            errors.append(f'{acid} configuration history is {sorted(history)}, expected {configuration}')
        row = latest.get(acid)
        if row is None:
            errors.append(f'{acid} has no final sample')
            continue
        values = dict(row)
        values['configuration'] = row.get('envelope_lateral_configuration')
        values['high_lift_id'] = CONFIG[configuration]['hlid']
        values['landing_gear'] = CONFIG[configuration]['gear']
        values['minimum_limit_name'] = 'nf3'
        values['maximum_limit_name'] = 'nf1'
        bounds = _check_bounds(values, acid, configuration, errors)
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
            errors.append(f'{acid} did not retain the excessive bank/load request')
        if policy == 'ENFORCE' and not (bank <= bank_max + 0.01 and load <= maximum + 0.001):
            errors.append(f'{acid} exceeded its enforced bank/load maximum')
    if metadata.get('event_total') != 4:
        errors.append(f'metadata event total is not 4: {metadata.get("event_total")}')
    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(errors)
    return (f'VALID: {len(rows)} samples, TO and LD REPORT/ENFORCE are '
            'configuration-stable, isolated, and transition-only')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('csv')
    parser.add_argument('--abort', choices=('TO', 'LD'))
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
