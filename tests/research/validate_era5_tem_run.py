#!/usr/bin/env python3
"""Validate recorded ERA5 application to matched BADA 4 TEM aircraft."""

import csv
import json
import math
from collections import defaultdict
from pathlib import Path
import sys


ACIDS = {'ERAR', 'ERAO'}
NUMERIC_ATMOS = ('temperature_k', 'pressure_pa', 'density_kg_m3',
                 'wind_north_m_s', 'wind_east_m_s', 'pressure_alt_m',
                 'tas_m_s', 'cas_m_s', 'mach')


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
    if metadata.get('schema_version') != 'samples-v7':
        errors.append('metadata schema is not samples-v7')

    for acid in ACIDS:
        samples = sorted(by_acid[acid], key=lambda row: float(row['sim_time_s']))
        if len(samples) < 15:
            errors.append(f'{acid} has only {len(samples)} samples')
            continue
        expected_policy = 'REPORT' if acid == 'ERAR' else 'OFF'
        if {row['envelope_policy'] for row in samples} != {expected_policy}:
            errors.append(f'{acid} does not retain {expected_policy} policy')
        if {row['dynamics_mode'] for row in samples} != {'TEM'}:
            errors.append(f'{acid} does not retain TEM dynamics')
        if {row['atmosphere_source'] for row in samples} != {'ERA5'}:
            errors.append(f'{acid} contains non-ERA5 atmosphere samples')
        if {row['atmosphere_valid'].lower() for row in samples} != {'true'}:
            errors.append(f'{acid} contains invalid atmosphere samples')
        if any(not row['dataset_time'].startswith('2025-08-15T12:00:00') for row in samples):
            errors.append(f'{acid} has unexpected ERA5 provenance time')
        if any(row['fallback_reason'] for row in samples):
            errors.append(f'{acid} contains atmosphere fallback reasons')
        if any(row['performance_miss_count'] not in ('', '0') for row in samples):
            errors.append(f'{acid} contains BADA evaluation misses')
        for row in samples:
            values = {field: float(row[field]) for field in NUMERIC_ATMOS}
            if not all(math.isfinite(value) for value in values.values()):
                errors.append(f'{acid} contains non-finite atmosphere or airdata')
                break
            if values['temperature_k'] <= 0 or values['pressure_pa'] <= 0 \
                    or values['density_kg_m3'] <= 0:
                errors.append(f'{acid} contains non-physical atmosphere values')
                break
            expected_rho = values['pressure_pa'] / (287.05287 * values['temperature_k'])
            if not math.isclose(values['density_kg_m3'], expected_rho, rel_tol=2e-6):
                errors.append(f'{acid} density is inconsistent with pressure and temperature')
                break
        masses = [float(row['mass_kg']) for row in samples]
        if any(after > before + 1e-6 for before, after in zip(masses, masses[1:])):
            errors.append(f'{acid} mass increases during the run')
        if acid == 'ERAO' and any(row['envelope_event_count'] not in ('', '0') for row in samples):
            errors.append('ERAO OFF samples contain envelope events')

    report = {row['sim_time_s']: row for row in by_acid['ERAR']}
    off = {row['sim_time_s']: row for row in by_acid['ERAO']}
    common = sorted(set(report).intersection(off), key=float)
    if len(common) < 15:
        errors.append(f'only {len(common)} time-aligned samples')
    maxima = {}
    tolerances = {'lat_deg': 1e-7, 'lon_deg': 1e-7, 'geometric_alt_m': 0.05,
                  'cas_m_s': 0.02, 'temperature_k': 1e-6, 'pressure_pa': 1e-3,
                  'wind_north_m_s': 1e-6, 'wind_east_m_s': 1e-6}
    for field, tolerance in tolerances.items():
        differences = [abs(float(report[t][field]) - float(off[t][field])) for t in common]
        if differences:
            maxima[field] = max(differences)
            if maxima[field] > tolerance:
                errors.append(f'REPORT/OFF {field} difference {maxima[field]:.6g} exceeds {tolerance}')

    if errors:
        return 'INVALID ERA5/TEM evidence:\n  - ' + '\n  - '.join(errors)
    metrics = ', '.join(f'{key}={value:.6g}' for key, value in maxima.items())
    return f'VALID: {len(rows)} ERA5/TEM samples; matched REPORT/OFF maxima: {metrics}'


def main():
    try:
        result = validate(sys.argv[1])
    except (IndexError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        result = f'INVALID ERA5/TEM evidence:\n  - evidence could not be validated: {exc}'
    print(result)
    if result.startswith('INVALID'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
