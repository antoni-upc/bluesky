#!/usr/bin/env python3
"""Validate the licensed BADA 3 four-aircraft route comparison."""

import csv
import json
import math
from collections import defaultdict
from pathlib import Path
import sys


ACIDS = {'B3RN', 'B3ON', 'B3RF', 'B3OF'}
PAIRS = (('B3RN', 'B3ON'), ('B3RF', 'B3OF'))
FINITE = ('sim_time_s', 'lat_deg', 'lon_deg', 'geometric_alt_m', 'tas_m_s',
          'cas_m_s', 'mach', 'vertical_speed_m_s', 'track_deg', 'temperature_k',
          'pressure_pa', 'density_kg_m3', 'thrust_n', 'rated_thrust_n', 'drag_n',
          'fuel_flow_kg_s', 'mass_kg')


def angle_difference(a, b):
    return abs((a - b + 180.0) % 360.0 - 180.0)


def number(row, field):
    return float(row[field])


def validate(path):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    raw = path.with_suffix('.events.jsonl').read_text(encoding='utf-8')
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    errors = []
    if metadata.get('schema_version') != 'samples-v9':
        errors.append('metadata schema is not samples-v9')
    if metadata.get('scenario') != 'pybada3-route':
        errors.append('metadata scenario is not pybada3-route')
    if metadata.get('event_total') != len(events):
        errors.append('metadata event total does not match the event ledger')
    if metadata.get('rows') != len(rows):
        errors.append('metadata row total does not match the CSV')
    if metadata.get('sample_intervals_s') != [1.0]:
        errors.append('sample interval is not exactly one second')
    if not raw.endswith('\n'):
        errors.append('event ledger is not newline-flushed')

    def finite_tree(value, name):
        if isinstance(value, dict):
            for key, item in value.items():
                finite_tree(item, f'{name}.{key}')
        elif isinstance(value, list):
            for index, item in enumerate(value):
                finite_tree(item, f'{name}[{index}]')
        elif isinstance(value, float) and not math.isfinite(value):
            errors.append(f'{name} is non-finite')
    finite_tree(events, 'events')
    finite_tree(metadata, 'metadata')

    by_acid = defaultdict(list)
    for row in rows:
        by_acid[row.get('acid')].append(row)
        for field in FINITE:
            try:
                if not math.isfinite(number(row, field)):
                    errors.append(f'{row.get("acid")} has non-finite {field}')
            except (KeyError, TypeError, ValueError):
                errors.append(f'{row.get("acid")} lacks numeric {field}')
    if set(by_acid) != ACIDS:
        errors.append(f'sampled aircraft are {sorted(by_acid)}, expected {sorted(ACIDS)}')

    policies = {'B3RN': 'REPORT', 'B3RF': 'REPORT', 'B3ON': 'OFF', 'B3OF': 'OFF'}
    effective = {item.get('aircraft'): item for item in metadata.get('effective_envelope', [])}
    for acid in ACIDS:
        samples = sorted(by_acid[acid], key=lambda row: number(row, 'sim_time_s'))
        by_acid[acid] = samples
        if len(samples) < 750 or (samples and
                number(samples[-1], 'sim_time_s') - number(samples[0], 'sim_time_s') < 750):
            errors.append(f'{acid} does not contain the complete route duration')
        item = effective.get(acid)
        if item is None or item.get('policy') != policies[acid]:
            errors.append(f'{acid} final metadata policy is not {policies[acid]}')
        elif item.get('configuration_mode') != 'PYBADA':
            errors.append(f'{acid} configuration mode is not PYBADA')
        if {row.get('envelope_policy') for row in samples} != {policies[acid]}:
            errors.append(f'{acid} sampled policy changed during the route')
        for row in samples:
            if (row.get('performance_model'), row.get('performance_aircraft'),
                    row.get('performance_resolution')) != (
                    'PYBADATEM-BADA3', 'A320__', 'bada3-code'):
                errors.append(f'{acid} lacks deterministic BADA 3 resolution')
                break
            if row.get('performance_valid', '').lower() not in ('true', '1') or \
                    row.get('performance_miss_count') != '0':
                errors.append(f'{acid} has an invalid or missed model evaluation')
                break

    for acid, initial_lat in (('B3RF', 41.240), ('B3OF', 41.280)):
        samples = by_acid[acid]
        if samples and max(abs(number(row, 'lat_deg') - initial_lat) for row in samples) > 0.002:
            errors.append(f'{acid} did not remain on its navigation-off latitude')
        if samples and max(angle_difference(number(row, 'track_deg'), 90.0)
                           for row in samples) > 1.0:
            errors.append(f'{acid} did not remain eastbound')
        if samples and max(abs(number(row, 'geometric_alt_m') - 914.4)
                           for row in samples) > 1.0:
            errors.append(f'{acid} did not retain 3,000 ft')
        if samples and max(abs(number(row, 'cas_m_s') - 92.59992)
                           for row in samples) > 0.1:
            errors.append(f'{acid} did not retain 180 kt CAS')

    for acid in ('B3RN', 'B3ON'):
        samples = by_acid[acid]
        tracks = [number(row, 'track_deg') for row in samples]
        sectors = (90.0, 0.0, 270.0, 180.0)
        if any(not any(angle_difference(track, sector) < 20.0 for track in tracks)
               for sector in sectors):
            errors.append(f'{acid} did not traverse all cardinal route sectors')
        if samples and max(number(row, 'lon_deg') for row in samples) < 2.23:
            errors.append(f'{acid} did not reach the east side of its route')
        if samples and max(number(row, 'geometric_alt_m') for row in samples) < 1750.0:
            errors.append(f'{acid} did not capture the 6,000-ft climb')
        final = samples[-20:]
        if final:
            mean_alt = sum(number(row, 'geometric_alt_m') for row in final) / len(final)
            mean_cas = sum(number(row, 'cas_m_s') for row in final) / len(final)
            if abs(mean_alt - 914.4) > 75.0:
                errors.append(f'{acid} did not capture final 3,000-ft constraint')
            if abs(mean_cas - 87.45548) > 3.0:
                errors.append(f'{acid} did not capture final 170-kt constraint')

    maxima = {}
    limits = {'latitude_deg': 0.001, 'longitude_deg': 0.001,
              'altitude_m': 2.0, 'cas_m_s': 0.5, 'track_deg': 1.0}
    for report, off in PAIRS:
        left = {row['sim_time_s']: row for row in by_acid[report]}
        right = {row['sim_time_s']: row for row in by_acid[off]}
        common = sorted(set(left).intersection(right), key=float)
        if len(common) < 750:
            errors.append(f'{report}/{off} lack 750 aligned samples')
            continue
        values = defaultdict(list)
        for timestamp in common:
            a, b = left[timestamp], right[timestamp]
            values['latitude_deg'].append(abs(number(a, 'lat_deg') - (number(b, 'lat_deg') - 0.040)))
            values['longitude_deg'].append(abs(number(a, 'lon_deg') - number(b, 'lon_deg')))
            values['altitude_m'].append(abs(number(a, 'geometric_alt_m') - number(b, 'geometric_alt_m')))
            values['cas_m_s'].append(abs(number(a, 'cas_m_s') - number(b, 'cas_m_s')))
            values['track_deg'].append(angle_difference(number(a, 'track_deg'), number(b, 'track_deg')))
        maxima[f'{report}/{off}'] = {field: max(samples) for field, samples in values.items()}
        for field, maximum in maxima[f'{report}/{off}'].items():
            if maximum > limits[field]:
                errors.append(f'{report}/{off} {field} difference {maximum:.6g} exceeds {limits[field]}')

    event_acids = {event.get('aircraft') for event in events}
    if not events or not {'B3RN', 'B3RF'}.issubset(event_acids):
        errors.append('one or more REPORT aircraft emitted no objective evidence')
    if not event_acids.issubset({'B3RN', 'B3RF'}):
        errors.append(f'OFF aircraft emitted events: {sorted(event_acids - {"B3RN", "B3RF"})}')
    if any(event.get('action') != 'ACCEPTED' or event.get('policy') != 'REPORT'
           for event in events):
        errors.append('route event ledger contains a non-REPORT acceptance')
    for acid in ('B3ON', 'B3OF'):
        if any(row.get('envelope_event_count') not in ('', '0') for row in by_acid[acid]):
            errors.append(f'{acid} OFF samples contain events')
    for acid in ('B3RN', 'B3RF'):
        expected = sum(event.get('aircraft') == acid for event in events)
        if by_acid[acid] and int(by_acid[acid][-1]['envelope_event_count']) != expected:
            errors.append(f'{acid} final event counter does not match its event ledger')

    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(dict.fromkeys(errors))
    metrics = '; '.join(
        f'{pair}: lat={value["latitude_deg"]:.6f} deg, lon={value["longitude_deg"]:.6f} deg, '
        f'alt={value["altitude_m"]:.2f} m, CAS={value["cas_m_s"]:.3f} m/s, '
        f'track={value["track_deg"]:.3f} deg' for pair, value in maxima.items())
    return (f'VALID: {len(rows)} finite BADA 3 samples, complete route and constraints pass; '
            f'navigation-off is stable; REPORT/OFF propagation is equivalent; {metrics}')


def main():
    try:
        result = validate(sys.argv[1])
    except (IndexError, OSError, ValueError, KeyError, json.JSONDecodeError,
            ZeroDivisionError) as exc:
        result = f'INVALID evidence:\n  - evidence could not be validated: {exc}'
    print(result)
    if result.startswith('INVALID'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
