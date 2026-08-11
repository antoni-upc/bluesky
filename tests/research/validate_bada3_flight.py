#!/usr/bin/env python3
"""Validate BADA 3 flight bounds without embedding licensed values."""

import csv
import argparse
import json
import math
from pathlib import Path


AIRCRAFT = {'B3HR': 'REPORT', 'B3HE': 'ENFORCE',
            'B3LR': 'REPORT', 'B3LE': 'ENFORCE'}
FINITE_FIELDS = (
    'sim_time_s', 'sample_interval_s', 'lat_deg', 'lon_deg',
    'geometric_alt_m', 'pressure_alt_m', 'tas_m_s', 'cas_m_s', 'mach',
    'vertical_speed_m_s', 'heading_deg', 'track_deg', 'temperature_k',
    'pressure_pa', 'density_kg_m3', 'wind_north_m_s', 'wind_east_m_s',
    'thrust_n', 'rated_thrust_n', 'drag_n', 'fuel_flow_kg_s', 'mass_kg',
    'mass_min_kg', 'mass_max_kg', 'minimum_cas_m_s', 'maximum_cas_m_s',
    'minimum_mach', 'maximum_mach', 'maximum_altitude_m',
    'minimum_rocd_m_s', 'maximum_rocd_m_s', 'bank_angle_deg', 'load_factor',
    'maximum_load_factor', 'maximum_bank_angle_deg')


def check_finite_rows(rows, errors):
    for row in rows:
        acid = row.get('acid', 'unknown')
        for field in FINITE_FIELDS:
            value = row.get(field)
            if value in (None, ''):
                errors.append(f'{acid} lacks significant numeric field {field}')
                continue
            try:
                if not math.isfinite(float(value)):
                    errors.append(f'{acid} has non-finite {field}={value}')
            except (TypeError, ValueError):
                errors.append(f'{acid} has non-numeric {field}={value}')


def check_finite_json(value, errors, location='evidence'):
    if isinstance(value, dict):
        for key, item in value.items():
            check_finite_json(item, errors, f'{location}.{key}')
    elif isinstance(value, list):
        for index, item in enumerate(value):
            check_finite_json(item, errors, f'{location}[{index}]')
    elif isinstance(value, float) and not math.isfinite(value):
        errors.append(f'{location} is non-finite')


def validate(path):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    raw_events = path.with_suffix('.events.jsonl').read_text(encoding='utf-8')
    events = [json.loads(line) for line in raw_events.splitlines() if line.strip()]
    errors = []
    check_finite_rows(rows, errors)
    check_finite_json(events, errors, 'events')
    check_finite_json(metadata, errors, 'metadata')
    if not raw_events.endswith('\n'):
        errors.append('event ledger is not newline-flushed')
    if metadata.get('schema_version') != 'samples-v7':
        errors.append('metadata schema is not samples-v7')
    if metadata.get('scenario') != 'pybada3-envelope-flight':
        errors.append('metadata scenario is not pybada3-envelope-flight')
    if metadata.get('quality_status') != 'DEGRADED':
        errors.append('metadata quality status is not DEGRADED')

    latest = {row.get('acid'): row for row in rows}
    for acid, policy in AIRCRAFT.items():
        aircraft_rows = [row for row in rows if row.get('acid') == acid]
        if not aircraft_rows:
            errors.append(f'{acid} has no samples')
            continue
        for row in aircraft_rows:
            if (row.get('performance_model'), row.get('performance_aircraft'),
                    row.get('performance_resolution')) != (
                    'PYBADATEM-BADA3', 'A320__', 'bada3-code'):
                errors.append(f'{acid} lacks deterministic BADA 3 resolution evidence')
            if row.get('performance_valid', '').lower() not in ('true', '1'):
                errors.append(f'{acid} has an invalid performance evaluation')
            if row.get('performance_miss_count') != '0':
                errors.append(f'{acid} has a nonzero performance miss count')
            try:
                values = [float(row[field]) for field in (
                    'minimum_cas_m_s', 'maximum_cas_m_s', 'minimum_mach',
                    'maximum_mach', 'maximum_altitude_m')]
                if not all(map(math.isfinite, values)):
                    errors.append(f'{acid} has a non-finite runtime flight bound')
                if not (0 < values[0] < values[1] and 0 < values[2] < values[3] < 1
                        and values[4] > 0):
                    errors.append(f'{acid} has contradictory runtime flight bounds')
            except (KeyError, TypeError, ValueError):
                errors.append(f'{acid} lacks numeric runtime flight bounds')
        if latest.get(acid, {}).get('envelope_policy') != policy:
            errors.append(f'{acid} final policy is not {policy}')

    for acid in ('B3HR', 'B3LR'):
        if latest.get(acid, {}).get('envelope_status') != 'INFEASIBLE':
            errors.append(f'{acid} REPORT state is not INFEASIBLE')
    for acid in ('B3HE', 'B3LE'):
        if latest.get(acid, {}).get('envelope_status') != 'VALID':
            errors.append(f'{acid} ENFORCE state is not VALID')

    actions = {(event.get('aircraft'), event.get('action')) for event in events}
    for expected in (('B3HR', 'ACCEPTED'), ('B3HE', 'LIMITED'),
                     ('B3LR', 'ACCEPTED'), ('B3LE', 'LIMITED')):
        if expected not in actions:
            errors.append(f'missing event {expected[0]} {expected[1]}')
    for event in events:
        reason = set(event.get('reason', '').split(','))
        requested, applied = event.get('requested', {}), event.get('applied', {})
        if event.get('action') == 'LIMITED':
            if reason.intersection({'HIGH_SPEED', 'MACH_MAX'}) and not (
                    requested.get('tas_m_s', 0) > applied.get('tas_m_s', math.inf)):
                errors.append(f'{event.get("aircraft")} high-speed limit did not reduce TAS')
            if 'ALTITUDE_MAX' in reason and not (
                    requested.get('altitude_m', 0) > applied.get('altitude_m', math.inf)):
                errors.append('B3HE altitude limit did not reduce altitude target')
            if reason.intersection({'LOW_SPEED', 'MACH_MIN'}) and not (
                    requested.get('tas_m_s', math.inf) < applied.get('tas_m_s', 0)):
                errors.append('B3LE low-speed limit did not increase TAS')
    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(dict.fromkeys(errors))
    return (f'VALID: {len(rows)} samples and {len(events)} runtime-relative flight-envelope '
            'events with isolated REPORT retention and ENFORCE limiting')


def validate_direct(path):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    raw_events = path.with_suffix('.events.jsonl').read_text(encoding='utf-8')
    events = [json.loads(line) for line in raw_events.splitlines() if line.strip()]
    errors = []
    check_finite_rows(rows, errors)
    check_finite_json(events, errors, 'events')
    check_finite_json(metadata, errors, 'metadata')
    if not raw_events.endswith('\n'):
        errors.append('event ledger is not newline-flushed')
    if metadata.get('schema_version') != 'samples-v7':
        errors.append('metadata schema is not samples-v7')
    if metadata.get('scenario') != 'pybada3-envelope-direct':
        errors.append('metadata scenario is not pybada3-envelope-direct')
    actions = [(event.get('aircraft'), event.get('action')) for event in events]
    expected = [('B3DR', 'ACCEPTED'), ('B3DE', 'REJECTED'), ('B3BAD', 'REJECTED')]
    if actions != expected:
        errors.append(f'direct event sequence is {actions}, expected {expected}')
    latest = {row.get('acid'): row for row in rows}
    if 'B3BAD' in latest:
        errors.append('rejected B3BAD creation appears in sampled traffic')
    report, enforce = latest.get('B3DR', {}), latest.get('B3DE', {})
    if (report.get('envelope_policy'), report.get('envelope_status')) != (
            'REPORT', 'INFEASIBLE'):
        errors.append('B3DR REPORT state is not sampled as INFEASIBLE')
    if (enforce.get('envelope_policy'), enforce.get('envelope_status')) != (
            'ENFORCE', 'VALID'):
        errors.append('B3DE ENFORCE state is not sampled as VALID')
    for acid, row in (('B3DR', report), ('B3DE', enforce)):
        if (row.get('performance_model'), row.get('performance_aircraft'),
                row.get('performance_resolution')) != (
                'PYBADATEM-BADA3', 'A320__', 'bada3-code'):
            errors.append(f'{acid} lacks deterministic BADA 3 resolution evidence')
        if row.get('performance_valid', '').lower() not in ('true', '1'):
            errors.append(f'{acid} has an invalid performance evaluation')
        if row.get('performance_miss_count') != '0':
            errors.append(f'{acid} has a nonzero performance miss count')
        try:
            altitude = float(row['geometric_alt_m'])
            maximum = float(row['maximum_altitude_m'])
            if not all(map(math.isfinite, (altitude, maximum))):
                errors.append(f'{acid} has non-finite altitude evidence')
            if acid == 'B3DR' and not altitude > maximum:
                errors.append('B3DR REPORT altitude is not above its runtime maximum')
            if acid == 'B3DE' and not altitude <= maximum:
                errors.append('B3DE rollback did not preserve altitude below its runtime maximum')
        except (KeyError, TypeError, ValueError):
            errors.append(f'{acid} lacks numeric direct-state altitude evidence')
    if len(events) == 3:
        rejected = events[1]
        requested, applied = rejected.get('requested', {}), rejected.get('applied', {})
        fields = ('altitude_m', 'cas_m_s', 'mach', 'vertical_rate_m_s')
        if not all(field in requested and field in applied for field in fields):
            errors.append('B3DE rejection lacks envelope-relevant requested/applied state')
        elif not requested['altitude_m'] > applied['altitude_m']:
            errors.append('B3DE rejection did not preserve the lower prior altitude')
        try:
            if (abs(float(enforce['lat_deg']) - 41.32) > 0.1 or
                    abs(float(enforce['lon_deg']) - 2.10) > 0.1 or
                    abs(float(enforce['heading_deg']) - 90.0) > 1e-6):
                errors.append('B3DE sampled position or heading indicates incomplete rollback')
        except (KeyError, TypeError, ValueError):
            errors.append('B3DE lacks sampled position/heading rollback evidence')
    if metadata.get('event_total') != 3:
        errors.append('metadata does not summarize three direct events')
    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(dict.fromkeys(errors))
    return (f'VALID: {len(rows)} samples, REPORT accepted and ENFORCE rolled back MOVE, '
            'and infeasible creation was transactional')


def validate_abort(path):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    raw_events = path.with_suffix('.events.jsonl').read_text(encoding='utf-8')
    events = [json.loads(line) for line in raw_events.splitlines() if line.strip()]
    errors = []
    check_finite_rows(rows, errors)
    check_finite_json(events, errors, 'events')
    check_finite_json(metadata, errors, 'metadata')
    if not raw_events.endswith('\n'):
        errors.append('event ledger is not newline-flushed')
    if len(events) != 1:
        errors.append(f'expected one ABORT event, got {len(events)}')
    else:
        event = events[0]
        if (event.get('aircraft'), event.get('policy'), event.get('action'),
                event.get('continuation')) != ('B3FA', 'ABORT', 'ABORTED', 'STOP'):
            errors.append('ABORT event identity/policy/action/continuation is incorrect')
        reasons = set(event.get('reason', '').split(','))
        if not reasons.intersection({'HIGH_SPEED', 'MACH_MAX'}):
            errors.append('ABORT event lacks a runtime high-speed reason')
        requested, applied = event.get('requested', {}), event.get('applied', {})
        if requested != applied:
            errors.append('ABORT event does not preserve requested state as applied')
    latest = rows[-1] if rows else {}
    if (latest.get('acid'), latest.get('performance_model'),
            latest.get('performance_aircraft'), latest.get('performance_resolution')) != (
            'B3FA', 'PYBADATEM-BADA3', 'A320__', 'bada3-code'):
        errors.append('final sample lacks deterministic BADA 3 resolution evidence')
    if latest.get('performance_valid', '').lower() not in ('true', '1'):
        errors.append('final sample has an invalid performance evaluation')
    if latest.get('performance_miss_count') != '0':
        errors.append('final sample has a nonzero performance miss count')
    if (latest.get('envelope_policy'), latest.get('envelope_status'),
            latest.get('envelope_last_action')) != ('ABORT', 'INFEASIBLE', 'ABORTED'):
        errors.append('final sample is not ABORT/INFEASIBLE/ABORTED')
    try:
        maximum_cas = float(latest['maximum_cas_m_s'])
        maximum_mach = float(latest['maximum_mach'])
        requested = events[0].get('requested', {}) if len(events) == 1 else {}
        if not (float(requested.get('cas_m_s')) > maximum_cas or
                float(requested.get('mach')) > maximum_mach):
            errors.append('ABORT request is not above a runtime speed or Mach maximum')
    except (KeyError, TypeError, ValueError):
        errors.append('final sample lacks numeric runtime speed-bound evidence')
    if metadata.get('schema_version') != 'samples-v7':
        errors.append('metadata schema is not samples-v7')
    if metadata.get('scenario') != 'pybada3-envelope-flight-abort':
        errors.append('metadata scenario is not pybada3-envelope-flight-abort')
    if metadata.get('event_total') != 1 or metadata.get('quality_status') != 'ABORTED':
        errors.append('metadata is not finalized with one ABORTED event')
    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(dict.fromkeys(errors))
    return (f'VALID: {len(rows)} samples, BADA 3 flight ABORT evidence finalized '
            'synchronously before HOLD')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('csv', type=Path)
    parser.add_argument('--direct', action='store_true')
    parser.add_argument('--abort', action='store_true')
    args = parser.parse_args()
    try:
        result = (validate_abort(args.csv) if args.abort else
                  (validate_direct(args.csv) if args.direct else validate(args.csv)))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        result = f'INVALID evidence:\n  - evidence could not be validated: {exc}'
    print(result)
    if result.startswith('INVALID'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
