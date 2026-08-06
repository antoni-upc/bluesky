#!/usr/bin/env python3
"""Validate licensed interactive BADA bank/load-factor evidence."""

import argparse
import csv
import json
from pathlib import Path


def load(path):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    events_path = path.with_suffix('.events.jsonl')
    events = [json.loads(line) for line in
              events_path.read_text(encoding='utf-8').splitlines()]
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    return rows, events, metadata


def validate(path, abort=False):
    rows, events, metadata = load(path)
    errors = []
    if metadata.get('schema_version') != 'samples-v7':
        errors.append('metadata schema is not samples-v7')
    required = ('bank_angle_deg', 'load_factor', 'minimum_load_factor',
                'maximum_load_factor', 'maximum_bank_angle_deg',
                'envelope_lateral_configuration')
    if not rows:
        errors.append('no samples were recorded')
    elif any(field not in rows[0] for field in required):
        errors.append('one or more lateral samples-v7 columns are missing')
    if abort:
        matching = [event for event in events if event.get('aircraft') == 'LABT'
                    and event.get('action') == 'ABORTED'
                    and event.get('reason') == 'BANK_ANGLE,LOAD_FACTOR']
        if len(matching) != 1:
            errors.append(f'LABT expected one ABORTED transition event, found {len(matching)}')
        if metadata.get('quality_status') != 'ABORTED':
            errors.append('metadata quality status is not ABORTED')
    else:
        latest = {}
        for row in rows:
            latest[row['acid']] = row
        expected = {'LATR': ('REPORT', 'INFEASIBLE', 'ACCEPTED'),
                    'LATE': ('ENFORCE', 'VALID', 'LIMITED')}
        for acid, (policy, status, action) in expected.items():
            row = latest.get(acid)
            if row is None:
                errors.append(f'{acid} has no final sample')
                continue
            if (row.get('envelope_policy'), row.get('envelope_status')) != (policy, status):
                errors.append(f'{acid} final policy/status is not {policy}/{status}')
            matching = [event for event in events if event.get('aircraft') == acid
                        and event.get('reason') == 'BANK_ANGLE,LOAD_FACTOR']
            if len(matching) != 1:
                errors.append(f'{acid} expected one transition event, found {len(matching)}')
                continue
            event = matching[0]
            if event.get('action') != action:
                errors.append(f'{acid} event action is not {action}')
            requested = event.get('requested') or {}
            applied = event.get('applied') or {}
            try:
                requested_bank = float(requested['bank_angle_deg'])
                requested_load = float(requested['load_factor'])
                applied_bank = float(applied['bank_angle_deg'])
                applied_load = float(applied['load_factor'])
            except (KeyError, TypeError, ValueError):
                errors.append(f'{acid} event lacks numeric requested/applied bank and load')
                continue
            if policy == 'REPORT' and (abs(requested_bank - applied_bank) > 1e-6 or
                                       abs(requested_load - applied_load) > 1e-6):
                errors.append('LATR REPORT did not retain requested bank and load')
            if policy == 'ENFORCE' and not (applied_bank < requested_bank and
                                            applied_load < requested_load):
                errors.append('LATE ENFORCE did not reduce bank and load')
        report, enforce = latest.get('LATR'), latest.get('LATE')
        if report:
            if not (float(report['bank_angle_deg']) >
                    float(report['maximum_bank_angle_deg']) + 0.01):
                errors.append('LATR sample is not above its BADA bank maximum')
            if not (float(report['load_factor']) >
                    float(report['maximum_load_factor']) + 0.001):
                errors.append('LATR sample is not above its BADA load maximum')
        if enforce:
            if float(enforce['bank_angle_deg']) > float(enforce['maximum_bank_angle_deg']) + 0.01:
                errors.append('LATE sampled bank is above its enforced maximum')
            if float(enforce['load_factor']) > float(enforce['maximum_load_factor']) + 0.001:
                errors.append('LATE sampled load is above its enforced maximum')
    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(errors)
    if abort:
        return 'VALID: lateral-envelope ABORT evidence finalized before HOLD'
    return (f'VALID: {len(rows)} samples, BADA bank/load REPORT and ENFORCE '
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
