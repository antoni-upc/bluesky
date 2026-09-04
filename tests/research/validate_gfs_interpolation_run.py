#!/usr/bin/env python3
"""Validate opt-in GFS temporal interpolation during matched TEM flight."""

import csv
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import sys

from bluesky.plugins.windgfs import WindGFS


PROVENANCE = re.compile(
    r'^2025-08-15T12:00:00->2025-08-15T18:00:00@(?P<weight>0\.\d{6})$')


def validate(path, cache='cache/weather/gfs'):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    errors = []
    by_acid = defaultdict(list)
    for row in rows:
        by_acid[row.get('acid')].append(row)
    if set(by_acid) != {'GITR', 'GITO'}:
        errors.append(f'aircraft are {sorted(by_acid)}, expected GITR and GITO')
    if metadata.get('event_total') != 0:
        errors.append(f'expected no events, got {metadata.get("event_total")}')

    provider = object.__new__(WindGFS)
    provider.cache = Path(cache).resolve()
    slot0 = datetime(2025, 8, 15, 12, tzinfo=timezone.utc)
    slot1 = datetime(2025, 8, 15, 18, tzinfo=timezone.utc)
    first = provider._read(provider._location(slot0)[1], slot0.replace(tzinfo=None))
    second = provider._read(provider._location(slot1)[1], slot1.replace(tzinfo=None))

    for acid, samples in by_acid.items():
        expected_policy = 'REPORT' if acid == 'GITR' else 'OFF'
        if {row['envelope_policy'] for row in samples} != {expected_policy}:
            errors.append(f'{acid} does not retain {expected_policy}')
        if {row['atmosphere_source'] for row in samples} != {'GFS'}:
            errors.append(f'{acid} contains non-GFS samples')
        if {row['atmosphere_valid'].lower() for row in samples} != {'true'}:
            errors.append(f'{acid} contains invalid samples')
        if any(row['fallback_reason'] for row in samples):
            errors.append(f'{acid} contains fallback reasons')
        if any(row['performance_miss_count'] not in ('', '0') for row in samples):
            errors.append(f'{acid} contains BADA misses')
        if any(row['envelope_event_count'] not in ('', '0') for row in samples):
            errors.append(f'{acid} contains unexpected quality events')
        for row in samples:
            match = PROVENANCE.match(row['dataset_time'])
            if not match:
                errors.append(f'{acid} has invalid interpolation provenance {row["dataset_time"]!r}')
                break
            utc = datetime.fromisoformat(row['sim_utc'])
            slot_start = utc.replace(hour=12, minute=0, second=0, microsecond=0)
            expected_weight = (utc - slot_start).total_seconds() / (6.0 * 3600.0)
            weight = float(match.group('weight'))
            if not math.isclose(weight, expected_weight, abs_tol=5.1e-7):
                errors.append(f'{acid} blend {weight} does not match UTC fraction {expected_weight}')
                break
            point = ([float(row['lat_deg'])], [float(row['lon_deg'])],
                     [float(row['geometric_alt_m'])])
            n0, e0, a0 = first.interpolate(*point)
            n1, e1, a1 = second.interpolate(*point)
            expected = {
                'temperature_k': (1 - weight) * a0.temperature[0] + weight * a1.temperature[0],
                'pressure_pa': (1 - weight) * a0.pressure[0] + weight * a1.pressure[0],
                'wind_north_m_s': (1 - weight) * n0[0] + weight * n1[0],
                'wind_east_m_s': (1 - weight) * e0[0] + weight * e1[0],
            }
            tolerances = {'temperature_k': 2e-5, 'pressure_pa': 2e-3,
                          'wind_north_m_s': 2e-5, 'wind_east_m_s': 2e-5}
            for field, value in expected.items():
                if not math.isclose(float(row[field]), value, abs_tol=tolerances[field]):
                    errors.append(f'{acid} {field} is not the independently computed blend')
                    break
            rho = float(row['pressure_pa']) / (287.05287 * float(row['temperature_k']))
            if not math.isclose(float(row['density_kg_m3']), rho, rel_tol=2e-6):
                errors.append(f'{acid} interpolated density is inconsistent with p/(R*T)')
                break

    left = {row['sim_time_s']: row for row in by_acid['GITR']}
    right = {row['sim_time_s']: row for row in by_acid['GITO']}
    common = set(left).intersection(right)
    fields = ('lat_deg', 'lon_deg', 'geometric_alt_m', 'cas_m_s',
              'temperature_k', 'pressure_pa', 'wind_north_m_s', 'wind_east_m_s')
    if len(common) < 15:
        errors.append(f'only {len(common)} aligned samples')
    elif any(abs(float(left[t][field]) - float(right[t][field])) > 1e-9
             for t in common for field in fields):
        errors.append('REPORT/OFF propagation or atmosphere differs')
    if errors:
        return 'INVALID GFS interpolation evidence:\n  - ' + '\n  - '.join(errors)
    return (f'VALID: {len(rows)} samples, UTC-derived GFS temporal interpolation; '
            'independent field checks and matched TEM propagation pass')


def main():
    try:
        result = validate(sys.argv[1])
    except (ImportError, IndexError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        result = f'INVALID GFS interpolation evidence:\n  - evidence could not be validated: {exc}'
    print(result)
    if result.startswith('INVALID'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
