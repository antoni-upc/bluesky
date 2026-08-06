"""Validate evidence produced by the interactive mass-envelope scenarios."""

import argparse
import csv
import json
from pathlib import Path


class EvidenceValidator:
    def __init__(self):
        self.errors = []

    def check(self, condition, message):
        if not condition:
            self.errors.append(message)

    def read_json(self, path):
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            self.errors.append(f'{path}: cannot read valid JSON: {exc}')
            return {}

    def read_events(self, path):
        try:
            raw = path.read_text(encoding='utf-8')
        except OSError as exc:
            self.errors.append(f'{path}: cannot read events: {exc}')
            return []
        self.check(raw.endswith('\n'), f'{path}: final event is not newline-flushed')
        events = []
        for number, line in enumerate(raw.splitlines(), 1):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                self.errors.append(f'{path}:{number}: invalid JSON: {exc}')
        return events

    def read_rows(self, path):
        try:
            with path.open(newline='', encoding='utf-8') as stream:
                return list(csv.DictReader(stream))
        except OSError as exc:
            self.errors.append(f'{path}: cannot read CSV: {exc}')
            return []

    def finish(self, success):
        if self.errors:
            details = '\n'.join(f'  - {error}' for error in self.errors)
            raise RuntimeError(f'INVALID evidence ({len(self.errors)} issue(s)):\n{details}')
        return success


def number(row, key, validator):
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        validator.errors.append(f'{row.get("acid", "unknown")}: invalid {key!r} value')
        return float('nan')


def validate_normal(csv_path):
    check = EvidenceValidator()
    events = check.read_events(csv_path.with_suffix('.events.jsonl'))
    check.check(len(events) == 3, f'expected 3 transition events, got {len(events)}')
    if len(events) == 3:
        check.check([e.get('reason') for e in events] == ['MASS_MAX'] * 3,
                    'event reasons/order differ from three MASS_MAX transitions')
        check.check([e.get('action') for e in events] ==
                    ['ACCEPTED', 'REJECTED', 'ACCEPTED'],
                    'event actions/order are not ACCEPTED, REJECTED, ACCEPTED')
        check.check([e.get('aircraft') for e in events] == ['MASSR', 'MASSE', 'MASSR'],
                    'event aircraft/order are not MASSR, MASSE, MASSR')
        for index, event in enumerate(events):
            requested, applied = event.get('requested'), event.get('applied')
            check.check(isinstance(requested, (int, float)) and
                        isinstance(applied, (int, float)),
                        f'event {index + 1} lacks numeric requested/applied mass')
        check.check(events[1].get('requested', 0) > events[1].get('applied', float('inf')),
                    'ENFORCE event did not preserve a lower applied mass')

    rows = check.read_rows(csv_path)
    latest = {row.get('acid'): row for row in rows}
    check.check('MASSR' in latest, 'CSV has no MASSR samples')
    check.check('MASSE' in latest, 'CSV has no MASSE samples')
    if 'MASSR' in latest:
        row = latest['MASSR']
        mass, maximum = number(row, 'mass_kg', check), number(row, 'mass_max_kg', check)
        check.check(mass > maximum, f'MASSR final mass {mass} is not above MTOW {maximum}')
        check.check(row.get('envelope_status') == 'INFEASIBLE',
                    'MASSR final status is not INFEASIBLE')
        check.check(row.get('envelope_policy') == 'REPORT',
                    'MASSR final policy is not REPORT')
        check.check(row.get('envelope_event_count') == '2',
                    'MASSR event counter is not 2')
    if 'MASSE' in latest:
        row = latest['MASSE']
        mass, maximum = number(row, 'mass_kg', check), number(row, 'mass_max_kg', check)
        check.check(mass <= maximum, f'MASSE final mass {mass} exceeds MTOW {maximum}')
        check.check(row.get('envelope_status') == 'VALID',
                    'MASSE final status is not VALID after rejection')
        check.check(row.get('envelope_policy') == 'ENFORCE',
                    'MASSE final policy is not ENFORCE')
        check.check(row.get('envelope_event_count') == '1',
                    'MASSE event counter is not 1')

    metadata = check.read_json(csv_path.with_suffix('.metadata.json'))
    check.check(metadata.get('schema_version') == 'samples-v5',
                'metadata schema is not samples-v5')
    check.check(metadata.get('event_total') == 3, 'metadata event_total is not 3')
    check.check(metadata.get('reason_totals') == {'MASS_MAX': 3},
                'metadata reason totals are not three MASS_MAX events')
    check.check(metadata.get('quality_status') == 'DEGRADED',
                'metadata quality status is not DEGRADED')
    return check.finish(
        f'VALID: {len(rows)} samples, fuel-aware bounds, 3 transitions, isolated ENFORCE rejection')


def validate_abort(csv_path):
    check = EvidenceValidator()
    events = check.read_events(csv_path.with_suffix('.events.jsonl'))
    check.check(len(events) == 1, f'expected one ABORT event, got {len(events)}')
    if len(events) == 1:
        event = events[0]
        expected = {'aircraft': 'MASSA', 'reason': 'MASS_MAX', 'policy': 'ABORT',
                    'action': 'ABORTED', 'continuation': 'STOP'}
        for key, value in expected.items():
            check.check(event.get(key) == value,
                        f'ABORT event {key} is {event.get(key)!r}, expected {value!r}')
    rows = check.read_rows(csv_path)
    latest = rows[-1] if rows else {}
    check.check(latest.get('acid') == 'MASSA', 'final ABORT CSV row is not MASSA')
    check.check(latest.get('envelope_status') == 'INFEASIBLE',
                'final ABORT CSV row does not capture INFEASIBLE state')
    check.check(latest.get('envelope_last_action') == 'ABORTED',
                'final ABORT CSV row does not capture ABORTED action')
    if latest:
        check.check(number(latest, 'mass_kg', check) > number(latest, 'mass_max_kg', check),
                    'final ABORT CSV mass is not above MTOW')
    metadata = check.read_json(csv_path.with_suffix('.metadata.json'))
    check.check(metadata.get('event_total') == 1, 'ABORT metadata event_total is not 1')
    check.check(metadata.get('quality_status') == 'ABORTED',
                'ABORT metadata quality status is not ABORTED')
    return check.finish('VALID: post-violation CSV, JSONL, and ABORTED metadata finalized before HOLD')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('csv', type=Path)
    parser.add_argument('--abort', action='store_true')
    args = parser.parse_args()
    try:
        message = validate_abort(args.csv) if args.abort else validate_normal(args.csv)
    except RuntimeError as exc:
        parser.exit(1, f'{exc}\n')
    print(message)


if __name__ == '__main__':
    main()
