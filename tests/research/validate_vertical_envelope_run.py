#!/usr/bin/env python3
"""Validate licensed interactive ROC/ROD envelope evidence."""

import argparse
import csv
import json
from pathlib import Path


def load(path):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    events_path = path.with_suffix('.events.jsonl')
    events = [json.loads(line) for line in events_path.read_text(encoding='utf-8').splitlines()]
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    return rows, events, metadata


def validate(path, abort=False, direct=False):
    rows, events, metadata = load(path)
    errors = []
    if metadata.get('schema_version') not in ('samples-v9', 'samples-v10'):
        errors.append('metadata schema is not compatible samples-v9/v10')
    if not rows:
        errors.append('no samples were recorded')
    for field in ('minimum_rocd_m_s', 'maximum_rocd_m_s'):
        if rows and field not in rows[0]:
            errors.append(f'{field} is missing')
    if abort:
        matching = [event for event in events if event.get('aircraft') == 'VABT'
                    and event.get('reason') == 'ROC_MAX'
                    and event.get('action') == 'ABORTED']
        if not matching:
            errors.append('VABT ROC_MAX ABORTED event is missing')
        if metadata.get('quality_status') != 'ABORTED':
            errors.append('metadata quality status is not ABORTED')
    elif direct:
        latest = {}
        for row in rows:
            latest[row['acid']] = row
        report, enforce = latest.get('VMR'), latest.get('VME')
        if report is None or enforce is None:
            errors.append('VMR or VME final sample is missing')
        else:
            if enforce.get('envelope_status') != 'VALID':
                errors.append('VME ENFORCE rollback is not VALID')
            if float(enforce['vertical_speed_m_s']) > float(enforce['maximum_rocd_m_s']):
                errors.append('VME prior VS was not preserved within its dynamic maximum')
            report_rows = [row for row in rows if row['acid'] == 'VMR']
            if not any(float(row['vertical_speed_m_s']) >
                       float(row['maximum_rocd_m_s']) + 0.01
                       for row in report_rows):
                errors.append('no sample captures VMR above its dynamic climb maximum')
        actions = {(event.get('aircraft'), event.get('action'), event.get('reason'))
                   for event in events}
        if ('VMR', 'ACCEPTED', 'ROC_MAX') not in actions:
            errors.append('VMR accepted ROC_MAX event is missing')
        if ('VME', 'REJECTED', 'ROC_MAX') not in actions:
            errors.append('VME rejected ROC_MAX event is missing')
    else:
        latest = {}
        for row in rows:
            latest[row['acid']] = row
        expected = {'VCR': ('REPORT', 'INFEASIBLE', 'ROC_MAX'),
                    'VCE': ('ENFORCE', 'VALID', 'ROC_MAX'),
                    'VDR': ('REPORT', 'INFEASIBLE', 'ROD_MAX'),
                    'VDE': ('ENFORCE', 'VALID', 'ROD_MAX')}
        for acid, (policy, status, reason) in expected.items():
            row = latest.get(acid)
            if row is None:
                errors.append(f'{acid} has no sample')
                continue
            if row.get('envelope_policy') != policy or row.get('envelope_status') != status:
                errors.append(f'{acid} final policy/status is not {policy}/{status}')
            matching = [event for event in events if event.get('aircraft') == acid
                        and event.get('reason') == reason]
            if len(matching) != 1:
                errors.append(f'{acid} expected one transition event, found {len(matching)}')
            else:
                event = matching[0]
                requested = float(event.get('requested', {}).get(
                    'vertical_rate_m_s', 'nan'))
                applied = float(event.get('applied', {}).get(
                    'vertical_rate_m_s', 'nan'))
                expected_sign = 1.0 if reason == 'ROC_MAX' else -1.0
                if requested * expected_sign <= 0.0:
                    errors.append(f'{acid} event requested VS has the wrong sign')
                if policy == 'REPORT':
                    if event.get('action') != 'ACCEPTED' or abs(requested - applied) > 1e-9:
                        errors.append(f'{acid} REPORT did not retain the requested signed VS')
                elif (event.get('action') != 'LIMITED' or
                      applied * expected_sign <= 0.0 or
                      abs(applied) >= abs(requested)):
                    errors.append(f'{acid} ENFORCE did not limit the signed VS')
        for acid in ('VCE', 'VDE'):
            row = latest.get(acid)
            if row:
                rate = float(row['vertical_speed_m_s'])
                low = float(row['minimum_rocd_m_s'])
                high = float(row['maximum_rocd_m_s'])
                if not low - 0.01 <= rate <= high + 0.01:
                    errors.append(f'{acid} sampled VS is outside enforced ROCD bounds')
    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(errors)
    if abort:
        return 'VALID: vertical-envelope ABORT evidence finalized before HOLD'
    if direct:
        return f'VALID: {len(rows)} samples, direct VS REPORT accepted and ENFORCE rolled back'
    return (f'VALID: {len(rows)} samples, directional ROC/ROD magnitude '
            'REPORT and ENFORCE are isolated')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('csv')
    parser.add_argument('--abort', action='store_true')
    parser.add_argument('--direct', action='store_true')
    args = parser.parse_args()
    try:
        result = validate(args.csv, args.abort, args.direct)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        result = f'INVALID evidence:\n  - evidence could not be validated: {exc}'
    print(result)
    if result.startswith('INVALID'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
