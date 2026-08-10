#!/usr/bin/env python3
"""Validate and summarize licensed BADA 4 terminal-configuration observations."""

import csv
import json
from pathlib import Path
import sys


PAIRS = {'takeoff candidate': (('TOB1', 'TOB2'), 'TO'),
         'landing candidate': (('LDB1', 'LDB2'), 'LD')}
EVIDENCE_FIELDS = ('configuration_mode', 'configuration', 'high_lift_id',
                   'landing_gear', 'minimum_limit_name', 'maximum_limit_name',
                   'minimum_load_factor', 'maximum_load_factor',
                   'maximum_bank_angle_deg')


def validate(path):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    events_path = path.with_suffix('.events.jsonl')
    events = [line for line in events_path.read_text(encoding='utf-8').splitlines()
              if line.strip()]
    errors = []
    if metadata.get('schema_version') != 'samples-v7':
        errors.append('metadata schema is not samples-v7')
    if metadata.get('scenario') != 'pybada-envelope-terminal-observe':
        errors.append('metadata scenario is not pybada-envelope-terminal-observe')
    if events or metadata.get('event_total') != 0:
        errors.append('observation-only gate emitted a QUALITY event')

    acids = {row.get('acid') for row in rows}
    expected_acids = {acid for pair, _ in PAIRS.values() for acid in pair}
    if acids != expected_acids:
        errors.append(f'sampled aircraft are {sorted(acids)}, expected {sorted(expected_acids)}')
    for row in rows:
        if row.get('envelope_policy') != 'OFF':
            errors.append(f'{row.get("acid")} has non-OFF envelope policy')
        try:
            if abs(float(row['bank_angle_deg'])) > 1e-6:
                errors.append(f'{row.get("acid")} has nonzero observed bank')
        except (KeyError, TypeError, ValueError):
            errors.append(f'{row.get("acid")} lacks numeric bank evidence')

    effective = {item.get('aircraft'): item
                 for item in metadata.get('effective_envelope', [])}
    summaries = []
    for label, (pair, expected_configuration) in PAIRS.items():
        sampled_configurations = {
            row.get('envelope_lateral_configuration')
            for row in rows if row.get('acid') in pair}
        flight_configurations = {
            row.get('envelope_configuration')
            for row in rows if row.get('acid') in pair}
        if sampled_configurations != {expected_configuration}:
            errors.append(
                f'{label} lateral configuration history is '
                f'{sorted(sampled_configurations)}, expected only {expected_configuration}')
        if flight_configurations != {expected_configuration}:
            errors.append(
                f'{label} flight configuration history is '
                f'{sorted(flight_configurations)}, expected only {expected_configuration}')
        items = [effective.get(acid) for acid in pair]
        if any(item is None for item in items):
            errors.append(f'{label} pair lacks final effective metadata')
            continue
        for acid, item in zip(pair, items):
            if item.get('policy') != 'OFF':
                errors.append(f'{acid} final metadata policy is not OFF')
            if item.get('configuration_mode') != 'PYBADA':
                errors.append(f'{acid} configuration mode is not PYBADA')
            for field in EVIDENCE_FIELDS[1:]:
                if item.get(field) in (None, ''):
                    errors.append(f'{acid} metadata lacks {field}')
        signatures = [tuple(item.get(field) for field in EVIDENCE_FIELDS) for item in items]
        if signatures[0] != signatures[1]:
            errors.append(f'{label} duplicated points do not expose identical metadata')
        item = items[0]
        if item.get('configuration') != expected_configuration:
            errors.append(
                f'{label} final configuration is {item.get("configuration")}, '
                f'expected {expected_configuration}')
        summaries.append(
            f'{label}: config={item.get("configuration")} '
            f'HLid={item.get("high_lift_id")} gear={item.get("landing_gear")} '
            f'DLM={item.get("minimum_limit_name")}/{item.get("maximum_limit_name")} '
            f'load={item.get("minimum_load_factor")}..{item.get("maximum_load_factor")} '
            f'bank_max={item.get("maximum_bank_angle_deg")} deg')
    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(errors)
    return (f'VALID: {len(rows)} samples, terminal observations are OFF, event-free, '
            'and pair-consistent\n  ' + '\n  '.join(summaries))


def main():
    try:
        result = validate(sys.argv[1])
    except (IndexError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        result = f'INVALID evidence:\n  - evidence could not be validated: {exc}'
    print(result)
    if result.startswith('INVALID'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
