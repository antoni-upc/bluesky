#!/usr/bin/env python3
"""Validate licensed BADA 4 high-lift lateral-envelope evidence."""

import csv
import json
import argparse
from pathlib import Path


def validate(path, abort=False):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    events = [json.loads(line) for line in
              path.with_suffix('.events.jsonl').read_text(encoding='utf-8').splitlines()]
    metadata = json.loads(
        path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    errors = []
    if metadata.get('schema_version') not in ('samples-v9', 'samples-v10'):
        errors.append('metadata schema is not compatible samples-v9/v10')
    expected_scenario = ('pybada-envelope-highlift-abort' if abort
                         else 'pybada-envelope-highlift')
    if metadata.get('scenario') != expected_scenario:
        errors.append(f'metadata scenario is not {expected_scenario}')
    latest = {}
    for row in rows:
        latest[row.get('acid')] = row
    if abort:
        row = latest.get('HLAB')
        if row is None:
            errors.append('HLAB has no finalized sample')
        else:
            if row.get('envelope_lateral_configuration') != 'IC':
                errors.append('HLAB final lateral configuration is not IC')
            try:
                minimum = float(row['minimum_load_factor'])
                maximum = float(row['maximum_load_factor'])
                bank_max = float(row['maximum_bank_angle_deg'])
            except (KeyError, TypeError, ValueError):
                errors.append('HLAB lacks numeric high-lift lateral evidence')
            else:
                if (abs(minimum) > 1e-6 or abs(maximum - 2.0) > 1e-6 or
                        abs(bank_max - 60.0) > 0.05):
                    errors.append('HLAB did not retain IC nf3/nf1 bounds')
        matching = [event for event in events if event.get('aircraft') == 'HLAB'
                    and event.get('action') == 'ABORTED'
                    and event.get('reason') == 'BANK_ANGLE,LOAD_FACTOR']
        if len(matching) != 1:
            errors.append(f'HLAB expected one ABORTED bank/load event, found {len(matching)}')
        else:
            requested = matching[0].get('requested') or {}
            applied = matching[0].get('applied') or {}
            try:
                if abs(float(requested['bank_angle_deg']) - 70.0) > 0.05:
                    errors.append('HLAB requested bank is not near 70 degrees')
                if abs(float(requested['load_factor']) - 2.9238044) > 0.005:
                    errors.append('HLAB requested load is not near 2.924')
                if requested != applied:
                    errors.append('HLAB ABORT evidence does not retain requested state')
            except (KeyError, TypeError, ValueError):
                errors.append('HLAB event lacks numeric requested/applied bank and load')
        if metadata.get('quality_status') != 'ABORTED':
            errors.append('metadata quality status is not ABORTED')
        if metadata.get('event_total') != 1:
            errors.append(f'metadata event total is not 1: {metadata.get("event_total")}')
        if errors:
            return 'INVALID evidence:\n  - ' + '\n  - '.join(errors)
        return 'VALID: high-lift IC ABORT evidence finalized before HOLD'
    expected = {'HLTR': ('REPORT', 'INFEASIBLE', 'ACCEPTED'),
                'HLTE': ('ENFORCE', 'VALID', 'LIMITED')}
    for acid, (policy, status, action) in expected.items():
        row = latest.get(acid)
        if row is None:
            errors.append(f'{acid} has no final sample')
            continue
        if row.get('envelope_lateral_configuration') != 'IC':
            errors.append(f'{acid} final lateral configuration is not IC')
        try:
            minimum = float(row['minimum_load_factor'])
            maximum = float(row['maximum_load_factor'])
            bank_max = float(row['maximum_bank_angle_deg'])
            bank = float(row['bank_angle_deg'])
            load = float(row['load_factor'])
        except (KeyError, TypeError, ValueError):
            errors.append(f'{acid} lacks numeric high-lift lateral evidence')
            continue
        if abs(minimum) > 1e-6 or abs(maximum - 2.0) > 1e-6:
            errors.append(f'{acid} did not select nf3/nf1 load bounds 0.0..2.0')
        if abs(bank_max - 60.0) > 0.05:
            errors.append(f'{acid} high-lift bank maximum is not near 60 degrees')
        if (row.get('envelope_policy'), row.get('envelope_status')) != (policy, status):
            errors.append(f'{acid} final policy/status is not {policy}/{status}')
        matching = [event for event in events if event.get('aircraft') == acid
                    and event.get('reason') == 'BANK_ANGLE,LOAD_FACTOR']
        if len(matching) != 1:
            errors.append(f'{acid} expected one bank/load event, found {len(matching)}')
        elif matching[0].get('action') != action:
            errors.append(f'{acid} event action is not {action}')
        if policy == 'REPORT' and not (bank > bank_max + 0.01 and load > maximum + 0.001):
            errors.append('HLTR did not retain the excessive bank/load request')
        if policy == 'ENFORCE' and not (bank <= bank_max + 0.01 and
                                        load <= maximum + 0.001):
            errors.append('HLTE exceeded its enforced high-lift bank/load maximum')
    if metadata.get('event_total') != 2:
        errors.append(f'metadata event total is not 2: {metadata.get("event_total")}')
    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(errors)
    return (f'VALID: {len(rows)} samples, high-lift IC REPORT and ENFORCE '
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
