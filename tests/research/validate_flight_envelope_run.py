"""Validate interactive CAS/Mach/altitude envelope evidence."""

import argparse
import csv
import json
from pathlib import Path


def fail(errors):
    if errors:
        raise RuntimeError('INVALID evidence:\n' + '\n'.join(f'  - {e}' for e in errors))


def read(path, errors):
    try:
        raw = path.read_text(encoding='utf-8')
    except OSError as exc:
        errors.append(f'{path}: {exc}')
        return []
    if not raw.endswith('\n'):
        errors.append(f'{path}: final event is not newline-flushed')
    try:
        return [json.loads(line) for line in raw.splitlines()]
    except json.JSONDecodeError as exc:
        errors.append(f'{path}: invalid JSONL: {exc}')
        return []


def validate(path, abort=False, direct=False):
    errors = []
    events = read(path.with_suffix('.events.jsonl'), errors)
    try:
        with path.open(newline='', encoding='utf-8') as stream:
            rows = list(csv.DictReader(stream))
    except OSError as exc:
        errors.append(f'{path}: {exc}')
        rows = []
    try:
        metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f'metadata unavailable: {exc}')
        metadata = {}

    if metadata.get('schema_version') != 'samples-v5':
        errors.append('schema is not samples-v5')
    if abort:
        if len(events) != 1:
            errors.append(f'expected 1 ABORT event, got {len(events)}')
        elif not (events[0].get('aircraft') == 'FLTA' and
                  events[0].get('action') == 'ABORTED' and
                  events[0].get('continuation') == 'STOP' and
                  {'HIGH_SPEED', 'MACH_MAX'}.intersection(events[0].get('reason', '').split(','))):
            errors.append(f'unexpected ABORT event: {events[0]}')
        if metadata.get('quality_status') != 'ABORTED':
            errors.append('metadata quality_status is not ABORTED')
        latest = rows[-1] if rows else {}
        if latest.get('envelope_last_action') != 'ABORTED':
            errors.append('final CSV row does not capture ABORTED action')
        fail(errors)
        return 'VALID: flight-envelope event and samples-v5 metadata finalized before HOLD'

    if direct:
        if len(events) != 3:
            errors.append(f'expected exactly 3 direct transition events, got {len(events)}')
        actions = [(event.get('aircraft'), event.get('action')) for event in events]
        if ('DRPT', 'ACCEPTED') not in actions:
            errors.append('DRPT direct REPORT event is missing')
        if ('DENF', 'REJECTED') not in actions:
            errors.append('DENF direct ENFORCE rejection event is missing')
        if ('CREBAD', 'REJECTED') not in actions:
            errors.append('CREBAD transactional creation rejection event is missing')
        latest = {row.get('acid'): row for row in rows}
        report = latest.get('DRPT', {})
        enforce = latest.get('DENF', {})
        if 'CREBAD' in latest:
            errors.append('CREBAD appears in sampled traffic after rejected CRE')
        if report.get('envelope_status') != 'INFEASIBLE':
            errors.append('DRPT accepted state is not sampled as INFEASIBLE')
        if enforce.get('envelope_status') != 'VALID':
            errors.append('DENF preserved state is not sampled as VALID')
        try:
            if float(report.get('geometric_alt_m', 0)) <= float(report.get('maximum_altitude_m', 'inf')):
                errors.append('DRPT altitude is not above its sampled maximum')
            if float(enforce.get('geometric_alt_m', 'inf')) > float(enforce.get('maximum_altitude_m', 0)):
                errors.append('DENF rollback did not preserve an altitude below maximum')
        except ValueError:
            errors.append('direct-state altitude evidence is not numeric')
        fail(errors)
        return f'VALID: {len(rows)} samples, REPORT accepted and ENFORCE rolled back direct MOVE'

    actions = [(event.get('aircraft'), event.get('action')) for event in events]
    if len(events) != 4:
        errors.append(f'expected exactly 4 guidance transition events, got {len(events)}')
    if ('FLTR', 'ACCEPTED') not in actions:
        errors.append('no FLTR REPORT acceptance event')
    if ('FLTE', 'LIMITED') not in actions:
        errors.append('no FLTE ENFORCE limiting event')
    for event in events:
        if event.get('aircraft') == 'FLTE' and event.get('action') == 'LIMITED':
            requested, applied = event.get('requested', {}), event.get('applied', {})
            if requested.get('tas_m_s', 0) <= applied.get('tas_m_s', float('inf')):
                errors.append('FLTE speed was not reduced in requested/applied evidence')
            if 'ALTITUDE_MAX' in event.get('reason', '') and \
                    requested.get('altitude_m', 0) <= applied.get('altitude_m', float('inf')):
                errors.append('FLTE altitude was not reduced in requested/applied evidence')
    latest = {row.get('acid'): row for row in rows}
    for acid in ('FLTR', 'FLTE'):
        row = latest.get(acid, {})
        for field in ('minimum_cas_m_s', 'maximum_cas_m_s', 'minimum_mach',
                      'maximum_mach', 'maximum_altitude_m', 'envelope_configuration'):
            if not row.get(field):
                errors.append(f'{acid} lacks sampled {field}')
    if latest.get('FLTR', {}).get('envelope_status') != 'INFEASIBLE':
        errors.append('FLTR final sampled status is not INFEASIBLE')
    if latest.get('FLTE', {}).get('envelope_status') != 'VALID':
        errors.append('FLTE final sampled status is not VALID')
    if metadata.get('quality_status') != 'DEGRADED':
        errors.append('metadata quality_status is not DEGRADED')
    fail(errors)
    return f'VALID: {len(rows)} samples, REPORT retained and ENFORCE atomically limited guidance'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('csv', type=Path)
    parser.add_argument('--abort', action='store_true')
    parser.add_argument('--direct', action='store_true')
    args = parser.parse_args()
    try:
        print(validate(args.csv, args.abort, args.direct))
    except RuntimeError as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
