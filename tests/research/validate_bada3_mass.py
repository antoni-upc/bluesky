#!/usr/bin/env python3
"""Validate BADA 3 mass behavior without embedding licensed model bounds."""

import csv
import argparse
import json
import math
from pathlib import Path


REPORT, ENFORCE = 'B3MR', 'B3ME'


def validate(path):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    raw_events = path.with_suffix('.events.jsonl').read_text(encoding='utf-8')
    events = [json.loads(line) for line in raw_events.splitlines() if line.strip()]
    errors = []
    if metadata.get('schema_version') != 'samples-v9':
        errors.append('metadata schema is not samples-v9')
    if metadata.get('scenario') != 'pybada3-envelope-mass':
        errors.append('metadata scenario is not pybada3-envelope-mass')
    if not raw_events.endswith('\n'):
        errors.append('event ledger is not newline-flushed')
    expected = [(REPORT, 'ACCEPTED'), (ENFORCE, 'REJECTED'), (REPORT, 'ACCEPTED')]
    if [(event.get('aircraft'), event.get('action')) for event in events] != expected:
        errors.append('events are not isolated ACCEPTED, REJECTED, ACCEPTED transitions')
    if any(event.get('reason') != 'MASS_MAX' for event in events):
        errors.append('an event has a reason other than MASS_MAX')
    if metadata.get('event_total') != 3 or metadata.get('reason_totals') != {'MASS_MAX': 3}:
        errors.append('metadata does not summarize exactly three MASS_MAX events')

    latest = {row.get('acid'): row for row in rows}
    for acid in (REPORT, ENFORCE):
        aircraft_rows = [row for row in rows if row.get('acid') == acid]
        if not aircraft_rows:
            errors.append(f'{acid} has no samples')
            continue
        for row in aircraft_rows:
            if row.get('performance_model') != 'PYBADATEM-BADA3':
                errors.append(f'{acid} did not use PYBADATEM-BADA3')
            if row.get('performance_aircraft') != 'A320__':
                errors.append(f'{acid} did not resolve to A320__')
            if row.get('performance_resolution') != 'bada3-code':
                errors.append(f'{acid} did not use bada3-code resolution')
            if row.get('performance_valid', '').lower() not in ('true', '1'):
                errors.append(f'{acid} has an invalid performance evaluation')
            if row.get('performance_miss_count') != '0':
                errors.append(f'{acid} has a nonzero performance miss count')
            try:
                minimum, maximum = float(row['mass_min_kg']), float(row['mass_max_kg'])
                if not all(map(math.isfinite, (minimum, maximum))) or not 0 < minimum < maximum:
                    errors.append(f'{acid} has invalid runtime mass bounds')
            except (KeyError, TypeError, ValueError):
                errors.append(f'{acid} lacks numeric runtime mass bounds')

    if REPORT in latest:
        row = latest[REPORT]
        if not float(row['mass_kg']) > float(row['mass_max_kg']):
            errors.append('REPORT final mass is not above its runtime maximum')
        if (row.get('envelope_policy'), row.get('envelope_status'),
                row.get('envelope_event_count')) != ('REPORT', 'INFEASIBLE', '2'):
            errors.append('REPORT final policy/status/counter is incorrect')
    if ENFORCE in latest:
        row = latest[ENFORCE]
        if not float(row['mass_kg']) <= float(row['mass_max_kg']):
            errors.append('ENFORCE did not preserve a mass within its runtime maximum')
        if (row.get('envelope_policy'), row.get('envelope_status'),
                row.get('envelope_event_count')) != ('ENFORCE', 'VALID', '1'):
            errors.append('ENFORCE final policy/status/counter is incorrect')
    if len(events) == 3:
        rejected = events[1]
        if not (isinstance(rejected.get('requested'), (int, float)) and
                isinstance(rejected.get('applied'), (int, float)) and
                rejected['requested'] > rejected['applied']):
            errors.append('ENFORCE event does not prove transactional preservation')
    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(dict.fromkeys(errors))
    return (f'VALID: {len(rows)} samples, runtime-derived mass bounds, three transitions, '
            'and isolated transactional ENFORCE rejection')


def validate_abort(path):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    raw_events = path.with_suffix('.events.jsonl').read_text(encoding='utf-8')
    events = [json.loads(line) for line in raw_events.splitlines() if line.strip()]
    errors = []
    if not raw_events.endswith('\n'):
        errors.append('event ledger is not newline-flushed')
    if len(events) != 1:
        errors.append(f'expected one ABORT event, got {len(events)}')
    else:
        event = events[0]
        expected = {'aircraft': 'B3MA', 'reason': 'MASS_MAX', 'policy': 'ABORT',
                    'action': 'ABORTED', 'continuation': 'STOP'}
        for key, value in expected.items():
            if event.get(key) != value:
                errors.append(f'ABORT event {key} is not {value}')
        requested, applied = event.get('requested'), event.get('applied')
        if not (isinstance(requested, (int, float)) and requested == applied):
            errors.append('ABORT event does not preserve its requested/applied mass')
    latest = rows[-1] if rows else {}
    if latest.get('acid') != 'B3MA':
        errors.append('final CSV row is not B3MA')
    if latest.get('performance_model') != 'PYBADATEM-BADA3':
        errors.append('final CSV row did not use PYBADATEM-BADA3')
    if latest.get('performance_aircraft') != 'A320__':
        errors.append('final CSV row did not resolve to A320__')
    if latest.get('performance_resolution') != 'bada3-code':
        errors.append('final CSV row did not use bada3-code resolution')
    if latest.get('performance_valid', '').lower() not in ('true', '1'):
        errors.append('final CSV row has an invalid performance evaluation')
    if latest.get('performance_miss_count') != '0':
        errors.append('final CSV row has a nonzero performance miss count')
    if (latest.get('envelope_policy'), latest.get('envelope_status'),
            latest.get('envelope_last_action')) != ('ABORT', 'INFEASIBLE', 'ABORTED'):
        errors.append('final CSV policy/status/action is not ABORT/INFEASIBLE/ABORTED')
    if latest:
        try:
            mass, minimum, maximum = (float(latest[key]) for key in
                                      ('mass_kg', 'mass_min_kg', 'mass_max_kg'))
            if not (all(map(math.isfinite, (mass, minimum, maximum))) and
                    0 < minimum < maximum < mass):
                errors.append('final mass is not above valid runtime-derived bounds')
        except (KeyError, TypeError, ValueError):
            errors.append('final CSV lacks numeric runtime mass evidence')
    if metadata.get('schema_version') != 'samples-v9':
        errors.append('metadata schema is not samples-v9')
    if metadata.get('scenario') != 'pybada3-envelope-mass-abort':
        errors.append('metadata scenario is not pybada3-envelope-mass-abort')
    if metadata.get('event_total') != 1 or metadata.get('reason_totals') != {'MASS_MAX': 1}:
        errors.append('metadata does not summarize exactly one MASS_MAX event')
    if metadata.get('quality_status') != 'ABORTED':
        errors.append('metadata quality status is not ABORTED')
    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(dict.fromkeys(errors))
    return (f'VALID: {len(rows)} samples, BADA 3 mass ABORT evidence finalized '
            'synchronously before HOLD')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('csv', type=Path)
    parser.add_argument('--abort', action='store_true')
    args = parser.parse_args()
    try:
        result = validate_abort(args.csv) if args.abort else validate(args.csv)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        result = f'INVALID evidence:\n  - evidence could not be validated: {exc}'
    print(result)
    if result.startswith('INVALID'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
