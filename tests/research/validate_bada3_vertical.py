#!/usr/bin/env python3
"""Validate runtime-derived licensed BADA 3 vertical-envelope evidence."""

import argparse
import csv
import json
import math
from pathlib import Path


EXPECTED = {'B3CR': ('REPORT', 'INFEASIBLE', 'ROC_MAX'),
            'B3CE': ('ENFORCE', 'VALID', 'ROC_MAX'),
            'B3DR': ('REPORT', 'INFEASIBLE', 'ROD_MAX'),
            'B3DE': ('ENFORCE', 'VALID', 'ROD_MAX')}
FINITE = ('sim_time_s', 'geometric_alt_m', 'tas_m_s', 'cas_m_s', 'mach',
          'vertical_speed_m_s', 'temperature_k', 'pressure_pa', 'density_kg_m3',
          'thrust_n', 'rated_thrust_n', 'drag_n', 'fuel_flow_kg_s', 'mass_kg',
          'minimum_rocd_m_s', 'maximum_rocd_m_s')


def load(path):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    raw = path.with_suffix('.events.jsonl').read_text(encoding='utf-8')
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    return rows, events, metadata, raw


def finite(rows, events, metadata, errors):
    for row in rows:
        for field in FINITE:
            try:
                if not math.isfinite(float(row[field])):
                    errors.append(f'{row.get("acid")} has non-finite {field}')
            except (KeyError, TypeError, ValueError):
                errors.append(f'{row.get("acid")} lacks numeric {field}')
    def walk(value, name):
        if isinstance(value, dict):
            for key, item in value.items(): walk(item, f'{name}.{key}')
        elif isinstance(value, list):
            for index, item in enumerate(value): walk(item, f'{name}[{index}]')
        elif isinstance(value, float) and not math.isfinite(value):
            errors.append(f'{name} is non-finite')
    walk(events, 'events')
    walk(metadata, 'metadata')


def validate(path, direct=False, abort=False):
    rows, events, metadata, raw = load(path)
    errors = []
    finite(rows, events, metadata, errors)
    if not raw.endswith('\n'):
        errors.append('event ledger is not newline-flushed')
    if metadata.get('schema_version') not in ('samples-v7', 'samples-v8', 'samples-v9', 'samples-v10'):
        errors.append('metadata schema is not compatible samples-v7/v8/v9/v10')
    suffix = '-abort' if abort else '-direct' if direct else ''
    scenario = f'pybada3-envelope-vertical{suffix}'
    if metadata.get('scenario') != scenario:
        errors.append(f'metadata scenario is not {scenario}')
    latest = {row.get('acid'): row for row in rows}
    if abort:
        matches = [event for event in events if event.get('aircraft') == 'B3VA'
                   and event.get('reason') == 'ROC_MAX'
                   and event.get('action') == 'ABORTED']
        if len(matches) != 1:
            errors.append(f'expected one B3VA ROC_MAX ABORTED event, got {len(matches)}')
        if metadata.get('quality_status') != 'ABORTED' or metadata.get('event_total') != 1:
            errors.append('metadata does not finalize one ABORTED transition')
        if errors:
            return 'INVALID evidence:\n  - ' + '\n  - '.join(dict.fromkeys(errors))
        return f'VALID: {len(rows)} samples, finite BADA 3 vertical ABORT evidence finalized before HOLD'
    if direct:
        report, enforce = latest.get('B3VR'), latest.get('B3VE')
        if report is None or enforce is None:
            errors.append('B3VR or B3VE final sample is missing')
        else:
            for acid, row in (('B3VR', report), ('B3VE', enforce)):
                if (row.get('performance_model'), row.get('performance_aircraft'),
                        row.get('performance_resolution')) != (
                        'PYBADATEM-BADA3', 'A320__', 'bada3-code'):
                    errors.append(f'{acid} lacks deterministic BADA 3 resolution')
                low, high = float(row['minimum_rocd_m_s']), float(row['maximum_rocd_m_s'])
                if not low <= high:
                    errors.append(f'{acid} has contradictory vertical bounds')
            if (report.get('envelope_policy'), report.get('envelope_status')) != ('REPORT', 'INFEASIBLE'):
                errors.append('B3VR is not REPORT/INFEASIBLE')
            if (enforce.get('envelope_policy'), enforce.get('envelope_status')) != ('ENFORCE', 'VALID'):
                errors.append('B3VE rollback is not ENFORCE/VALID')
            if float(enforce['vertical_speed_m_s']) > float(enforce['maximum_rocd_m_s']):
                errors.append('B3VE prior vertical rate was not preserved')
        actions = {(event.get('aircraft'), event.get('action'), event.get('reason'))
                   for event in events}
        if ('B3VR', 'ACCEPTED', 'ROC_MAX') not in actions:
            errors.append('B3VR accepted ROC_MAX event is missing')
        if ('B3VE', 'REJECTED', 'ROC_MAX') not in actions:
            errors.append('B3VE rejected ROC_MAX event is missing')
        if metadata.get('event_total') != 2:
            errors.append('metadata does not summarize two direct transitions')
        if errors:
            return 'INVALID evidence:\n  - ' + '\n  - '.join(dict.fromkeys(errors))
        return f'VALID: {len(rows)} samples, finite direct VS REPORT acceptance and ENFORCE rollback'
    for acid, (policy, status, reason) in EXPECTED.items():
        row = latest.get(acid)
        if row is None:
            errors.append(f'{acid} has no final sample')
            continue
        if (row.get('performance_model'), row.get('performance_aircraft'),
                row.get('performance_resolution')) != (
                'PYBADATEM-BADA3', 'A320__', 'bada3-code'):
            errors.append(f'{acid} lacks deterministic BADA 3 resolution')
        if row.get('performance_valid', '').lower() not in ('true', '1') or \
                row.get('performance_miss_count') != '0':
            errors.append(f'{acid} is invalid or has evaluation misses')
        if (row.get('envelope_policy'), row.get('envelope_status')) != (policy, status):
            errors.append(f'{acid} final policy/status is not {policy}/{status}')
        matches = [event for event in events if event.get('aircraft') == acid
                   and event.get('reason') == reason]
        if len(matches) != 1:
            errors.append(f'{acid} expected one {reason} transition, got {len(matches)}')
            continue
        event = matches[0]
        requested = float(event.get('requested', {}).get('vertical_rate_m_s', math.nan))
        applied = float(event.get('applied', {}).get('vertical_rate_m_s', math.nan))
        sign = 1 if reason == 'ROC_MAX' else -1
        if not math.isfinite(requested) or requested * sign <= 0:
            errors.append(f'{acid} requested vertical rate has wrong sign or is non-finite')
        if policy == 'REPORT' and not (event.get('action') == 'ACCEPTED' and requested == applied):
            errors.append(f'{acid} REPORT did not retain requested rate')
        if policy == 'ENFORCE' and not (event.get('action') == 'LIMITED' and
                applied * sign > 0 and abs(applied) < abs(requested)):
            errors.append(f'{acid} ENFORCE did not limit signed rate')
        low, high, rate = (float(row[key]) for key in
                           ('minimum_rocd_m_s', 'maximum_rocd_m_s', 'vertical_speed_m_s'))
        if not low <= high:
            errors.append(f'{acid} has contradictory vertical bounds')
        if policy == 'ENFORCE' and not low - 0.01 <= rate <= high + 0.01:
            errors.append(f'{acid} final rate is outside enforced bounds')
    if metadata.get('event_total') != 4:
        errors.append('metadata does not summarize four transitions')
    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(dict.fromkeys(errors))
    return (f'VALID: {len(rows)} samples, finite runtime ROC/ROD bounds, '
            'and isolated directional REPORT/ENFORCE behavior')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('csv', type=Path)
    parser.add_argument('--direct', action='store_true')
    parser.add_argument('--abort', action='store_true')
    args = parser.parse_args()
    try:
        result = validate(args.csv, args.direct, args.abort)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        result = f'INVALID evidence:\n  - evidence could not be validated: {exc}'
    print(result)
    if result.startswith('INVALID'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
