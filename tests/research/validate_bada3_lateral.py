#!/usr/bin/env python3
"""Validate runtime-derived licensed BADA 3 lateral-envelope evidence."""

import argparse
import csv
import json
import math
from pathlib import Path


FINITE = ('sim_time_s', 'geometric_alt_m', 'tas_m_s', 'cas_m_s', 'mach',
          'vertical_speed_m_s', 'temperature_k', 'pressure_pa', 'density_kg_m3',
          'thrust_n', 'rated_thrust_n', 'drag_n', 'fuel_flow_kg_s', 'mass_kg',
          'bank_angle_deg', 'load_factor', 'maximum_load_factor',
          'maximum_bank_angle_deg')


def load(path):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    raw = path.with_suffix('.events.jsonl').read_text(encoding='utf-8')
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    return rows, events, metadata, raw


def check_finite(rows, events, metadata, errors):
    for row in rows:
        for field in FINITE:
            try:
                if not math.isfinite(float(row[field])):
                    errors.append(f'{row.get("acid")} has non-finite {field}')
            except (KeyError, TypeError, ValueError):
                errors.append(f'{row.get("acid")} lacks numeric {field}')

    def walk(value, name):
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f'{name}.{key}')
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f'{name}[{index}]')
        elif isinstance(value, float) and not math.isfinite(value):
            errors.append(f'{name} is non-finite')
    walk(events, 'events')
    walk(metadata, 'metadata')


def check_resolution(acid, row, errors):
    if (row.get('performance_model'), row.get('performance_aircraft'),
            row.get('performance_resolution')) != (
            'PYBADATEM-BADA3', 'A320__', 'bada3-code'):
        errors.append(f'{acid} lacks deterministic BADA 3 resolution')
    if row.get('performance_valid', '').lower() not in ('true', '1') or \
            row.get('performance_miss_count') != '0':
        errors.append(f'{acid} is invalid or has evaluation misses')


def check_bounds(acid, row, errors):
    try:
        maximum_bank = float(row['maximum_bank_angle_deg'])
        maximum_load = float(row['maximum_load_factor'])
    except (KeyError, TypeError, ValueError):
        errors.append(f'{acid} lacks numeric lateral maximum bounds')
        return
    derived = 1.0 / math.cos(math.radians(maximum_bank))
    if abs(maximum_load - derived) > 1e-12:
        errors.append(f'{acid} load ceiling is not derived from its bank maximum')
    minimum = row.get('minimum_load_factor', '')
    if minimum not in ('', None):
        try:
            if float(minimum) < 0.0:
                errors.append(f'{acid} invents a negative BADA 3 load bound')
        except (TypeError, ValueError):
            errors.append(f'{acid} minimum load bound is neither absent nor numeric')


def check_transition(acid, row, rows, events, policy, status, action, config, errors):
    check_resolution(acid, row, errors)
    history = {item.get('envelope_lateral_configuration') for item in rows
               if item.get('acid') == acid}
    if history != {config}:
        errors.append(f'{acid} lateral configuration history is {sorted(history)}, expected {config}')
    check_bounds(acid, row, errors)
    if (row.get('envelope_policy'), row.get('envelope_status')) != (policy, status):
        errors.append(f'{acid} final policy/status is not {policy}/{status}')
    matches = [event for event in events if event.get('aircraft') == acid
               and event.get('reason') == 'BANK_ANGLE,LOAD_FACTOR']
    if len(matches) != 1:
        errors.append(f'{acid} expected one bank/load transition, got {len(matches)}')
        return
    event = matches[0]
    if event.get('action') != action:
        errors.append(f'{acid} action is not {action}')
        return
    try:
        requested_bank = float(event['requested']['bank_angle_deg'])
        requested_load = float(event['requested']['load_factor'])
        applied_bank = float(event['applied']['bank_angle_deg'])
        applied_load = float(event['applied']['load_factor'])
        maximum_bank = float(row['maximum_bank_angle_deg'])
        maximum_load = float(row['maximum_load_factor'])
    except (KeyError, TypeError, ValueError):
        errors.append(f'{acid} lacks numeric event or bound evidence')
        return
    if not all(map(math.isfinite, (requested_bank, requested_load, applied_bank,
                                   applied_load, maximum_bank, maximum_load))):
        errors.append(f'{acid} event or bound evidence is non-finite')
    if policy == 'REPORT' and not (requested_bank == applied_bank and
                                   requested_load == applied_load and
                                   applied_bank > maximum_bank and
                                   applied_load > maximum_load):
        errors.append(f'{acid} REPORT did not retain the excessive request')
    if policy == 'ENFORCE' and not (applied_bank < requested_bank and
                                    applied_load < requested_load and
                                    applied_bank <= maximum_bank + 0.01 and
                                    applied_load <= maximum_load + 0.001):
        errors.append(f'{acid} ENFORCE did not limit to runtime bounds')


def validate(path, terminal=False, abort=False, terminal_abort=False):
    rows, events, metadata, raw = load(path)
    errors = []
    check_finite(rows, events, metadata, errors)
    if not raw.endswith('\n'):
        errors.append('event ledger is not newline-flushed')
    if metadata.get('schema_version') not in ('samples-v7', 'samples-v8', 'samples-v9', 'samples-v10'):
        errors.append('metadata schema is not compatible samples-v7/v8/v9/v10')
    suffix = ('-terminal-abort' if terminal_abort else '-terminal' if terminal else
              '-lateral-abort' if abort else '-lateral')
    scenario = f'pybada3-envelope{suffix}'
    if metadata.get('scenario') != scenario:
        errors.append(f'metadata scenario is not {scenario}')
    latest = {row.get('acid'): row for row in rows}

    if abort or terminal_abort:
        acid = 'B3TA' if terminal_abort else 'B3LA'
        row = latest.get(acid)
        if row is None:
            errors.append(f'{acid} has no finalized sample')
        else:
            check_resolution(acid, row, errors)
            expected_config = 'TO' if terminal_abort else 'CR'
            history = {item.get('envelope_lateral_configuration') for item in rows
                       if item.get('acid') == acid}
            if history != {expected_config}:
                errors.append(f'{acid} lateral configuration history is not {expected_config}')
            check_bounds(acid, row, errors)
        matches = [event for event in events if event.get('aircraft') == acid
                   and event.get('reason') == 'BANK_ANGLE,LOAD_FACTOR'
                   and event.get('action') == 'ABORTED']
        if len(matches) != 1:
            errors.append(f'expected one {acid} ABORTED event, got {len(matches)}')
        elif matches[0].get('requested') != matches[0].get('applied'):
            errors.append(f'{acid} ABORT did not retain the requested state')
        if metadata.get('quality_status') != 'ABORTED' or metadata.get('event_total') != 1:
            errors.append('metadata does not finalize one ABORTED transition')
    else:
        expected = ({
            'B3TR': ('REPORT', 'INFEASIBLE', 'ACCEPTED', 'TO'),
            'B3TE': ('ENFORCE', 'VALID', 'LIMITED', 'TO'),
            'B3DR': ('REPORT', 'INFEASIBLE', 'ACCEPTED', 'LD'),
            'B3DE': ('ENFORCE', 'VALID', 'LIMITED', 'LD'),
        } if terminal else {
            'B3LR': ('REPORT', 'INFEASIBLE', 'ACCEPTED', 'CR'),
            'B3LE': ('ENFORCE', 'VALID', 'LIMITED', 'CR'),
        })
        for acid, values in expected.items():
            row = latest.get(acid)
            if row is None:
                errors.append(f'{acid} has no final sample')
                continue
            check_transition(acid, row, rows, events, *values, errors)
        if metadata.get('event_total') != len(expected):
            errors.append(f'metadata event total is not {len(expected)}')
        if terminal:
            for pair in (('B3TR', 'B3TE'), ('B3DR', 'B3DE')):
                try:
                    evidence = {(latest[acid]['envelope_lateral_configuration'],
                                 float(latest[acid]['maximum_bank_angle_deg']),
                                 float(latest[acid]['maximum_load_factor'])) for acid in pair}
                    if len(evidence) != 1:
                        errors.append(f'{pair} runtime limits are not pair-consistent')
                except KeyError:
                    pass

    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(dict.fromkeys(errors))
    label = ('terminal lateral ABORT finalized before HOLD' if terminal_abort else
             'terminal TO/LD REPORT and ENFORCE' if terminal else
             'lateral ABORT finalized before HOLD' if abort else
             'cruise REPORT and ENFORCE')
    return f'VALID: {len(rows)} finite samples, BADA 3 {label} evidence is runtime-derived and isolated'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('csv', type=Path)
    parser.add_argument('--terminal', action='store_true')
    parser.add_argument('--abort', action='store_true')
    parser.add_argument('--terminal-abort', action='store_true')
    args = parser.parse_args()
    if sum((args.terminal, args.abort, args.terminal_abort)) > 1:
        raise SystemExit('choose only one mode')
    try:
        result = validate(args.csv, args.terminal, args.abort, args.terminal_abort)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        result = f'INVALID evidence:\n  - evidence could not be validated: {exc}'
    print(result)
    if result.startswith('INVALID'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
