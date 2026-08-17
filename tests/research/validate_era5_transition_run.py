#!/usr/bin/env python3
"""Validate automatic ERA5 hourly transition during a matched TEM run."""

import csv
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import sys


ACIDS = {'EWTR', 'EWTO'}
EXPECTED_SLOTS = {'2025-08-15T12:00:00', '2025-08-15T13:00:00'}


def validate(path):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    errors = []
    by_acid = defaultdict(list)
    for row in rows:
        by_acid[row.get('acid')].append(row)
    if set(by_acid) != ACIDS:
        errors.append(f'aircraft are {sorted(by_acid)}, expected {sorted(ACIDS)}')
    if set(metadata.get('dataset_times', [])) != EXPECTED_SLOTS:
        errors.append(f'metadata slots are {metadata.get("dataset_times")}, expected both hourly slots')
    if metadata.get('event_total') != 0:
        errors.append(f'expected no envelope events, got {metadata.get("event_total")}')

    for acid in ACIDS:
        samples = sorted(by_acid[acid], key=lambda row: float(row['sim_time_s']))
        slots = [row['dataset_time'] for row in samples]
        if set(slots) != EXPECTED_SLOTS:
            errors.append(f'{acid} slots are {sorted(set(slots))}')
        elif slots != sorted(slots):
            errors.append(f'{acid} returned to an older ERA5 slot')
        for row in samples:
            utc = datetime.fromisoformat(row['sim_utc'])
            expected = utc.replace(minute=0, second=0, microsecond=0).isoformat()
            if row['dataset_time'] != expected:
                errors.append(
                    f'{acid} uses {row["dataset_time"]} at {row["sim_utc"]}; expected {expected}')
                break
        if {row['atmosphere_source'] for row in samples} != {'ERA5'}:
            errors.append(f'{acid} contains non-ERA5 samples')
        if {row['atmosphere_valid'].lower() for row in samples} != {'true'}:
            errors.append(f'{acid} contains invalid atmosphere samples')
        if any(row['fallback_reason'] for row in samples):
            errors.append(f'{acid} contains fallback reasons')
        if any(row['performance_miss_count'] not in ('', '0') for row in samples):
            errors.append(f'{acid} contains BADA misses')
        masses = [float(row['mass_kg']) for row in samples]
        if any(after > before + 1e-6 for before, after in zip(masses, masses[1:])):
            errors.append(f'{acid} mass increases')
        for row in samples:
            values = [float(row[field]) for field in
                      ('temperature_k', 'pressure_pa', 'density_kg_m3',
                       'wind_north_m_s', 'wind_east_m_s', 'pressure_alt_m',
                       'tas_m_s', 'cas_m_s', 'mach')]
            if not all(math.isfinite(value) for value in values):
                errors.append(f'{acid} contains non-finite atmosphere or airdata')
                break
        if acid == 'EWTO' and any(row['envelope_event_count'] not in ('', '0') for row in samples):
            errors.append('EWTO OFF samples contain envelope events')
        if any(row['envelope_event_count'] not in ('', '0') for row in samples):
            errors.append(f'{acid} contains unexpected envelope events')

    left = {row['sim_time_s']: row for row in by_acid['EWTR']}
    right = {row['sim_time_s']: row for row in by_acid['EWTO']}
    common = sorted(set(left).intersection(right), key=float)
    fields = ('lat_deg', 'lon_deg', 'geometric_alt_m', 'cas_m_s',
              'temperature_k', 'pressure_pa', 'wind_north_m_s', 'wind_east_m_s')
    maxima = {field: max((abs(float(left[t][field]) - float(right[t][field]))
                          for t in common), default=math.inf) for field in fields}
    if len(common) < 60:
        errors.append(f'only {len(common)} aligned samples')
    if any(value > 1e-9 for value in maxima.values()):
        errors.append(f'REPORT/OFF mismatch across transition: {maxima}')
    if errors:
        return 'INVALID ERA5 transition evidence:\n  - ' + '\n  - '.join(errors)
    return (f'VALID: {len(rows)} samples, automatic 12:00->13:00 ERA5 transition; '
            'no fallback or BADA misses; REPORT/OFF remains identical')


def main():
    try:
        result = validate(sys.argv[1])
    except (IndexError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        result = f'INVALID ERA5 transition evidence:\n  - evidence could not be validated: {exc}'
    print(result)
    if result.startswith('INVALID'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
