#!/usr/bin/env python3
"""Validate and summarize licensed BADA 3.15 phase/lateral observations."""

import csv
import json
import math
from pathlib import Path
import sys


PAIRS = {
    'takeoff': (('TO1', 'TO2'), 'TO'),
    'initial climb': (('IC1', 'IC2'), 'IC'),
    'cruise': (('CR1', 'CR2'), 'CR'),
    'approach': (('AP1', 'AP2'), 'AP'),
    'landing': (('LD1', 'LD2'), 'LD'),
}


def number(row, field, errors):
    try:
        value = float(row[field])
    except (KeyError, TypeError, ValueError):
        errors.append(f'{row.get("acid")} lacks numeric {field}')
        return None
    if not math.isfinite(value):
        errors.append(f'{row.get("acid")} has non-finite {field}')
        return None
    return value


def validate(path):
    path = Path(path)
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    events = [line for line in path.with_suffix('.events.jsonl').read_text(
        encoding='utf-8').splitlines() if line.strip()]
    errors = []
    expected_acids = {acid for pair, _ in PAIRS.values() for acid in pair}
    if metadata.get('schema_version') not in ('samples-v7', 'samples-v8', 'samples-v9', 'samples-v10'):
        errors.append('metadata schema is not compatible samples-v7/v8/v9/v10')
    if metadata.get('scenario') != 'pybada3-envelope-observe':
        errors.append('metadata scenario is not pybada3-envelope-observe')
    if events or metadata.get('event_total') != 0:
        errors.append('observation-only gate emitted a QUALITY event')
    if {row.get('acid') for row in rows} != expected_acids:
        errors.append('sampled aircraft do not match the five duplicated pairs')

    for row in rows:
        acid = row.get('acid')
        if row.get('performance_model') != 'PYBADATEM-BADA3':
            errors.append(f'{acid} did not use BADA3')
        if row.get('performance_dataset_version') != '3.15':
            errors.append(f'{acid} did not record dataset 3.15')
        if row.get('performance_aircraft') != 'A320__':
            errors.append(f'{acid} did not resolve to A320__')
        if row.get('performance_resolution') != 'bada3-code':
            errors.append(f'{acid} did not use bada3-code resolution')
        if row.get('performance_dummy', '').lower() not in ('false', '0'):
            errors.append(f'{acid} used a dummy model')
        if row.get('performance_valid', '').lower() not in ('true', '1'):
            errors.append(f'{acid} has an invalid performance evaluation')
        if row.get('performance_miss_count') != '0':
            errors.append(f'{acid} has a nonzero performance miss count')
        if row.get('envelope_policy') != 'OFF':
            errors.append(f'{acid} has non-OFF envelope policy')
        if row.get('envelope_event_count') != '0':
            errors.append(f'{acid} has an envelope event')
        if row.get('minimum_load_factor') not in ('', None):
            errors.append(f'{acid} invents a negative/minimum BADA 3 load bound')
        bank = number(row, 'maximum_bank_angle_deg', errors)
        load = number(row, 'maximum_load_factor', errors)
        observed_bank = number(row, 'bank_angle_deg', errors)
        if observed_bank is not None and abs(observed_bank) > 1e-6:
            errors.append(f'{acid} has nonzero observed bank')
        if bank is not None and not 0.0 <= bank < 90.0:
            errors.append(f'{acid} has invalid maximum bank {bank}')
        if bank is not None and load is not None:
            expected_load = 1.0 / math.cos(math.radians(bank))
            if not math.isclose(load, expected_load, rel_tol=1e-9, abs_tol=1e-12):
                errors.append(f'{acid} load ceiling is not derived from its bank maximum')

    summaries = []
    for label, (pair, expected_configuration) in PAIRS.items():
        pair_rows = [row for row in rows if row.get('acid') in pair]
        configs = {row.get('envelope_configuration') for row in pair_rows}
        lateral_configs = {row.get('envelope_lateral_configuration') for row in pair_rows}
        if configs != {expected_configuration} or lateral_configs != {expected_configuration}:
            errors.append(f'{label} configuration is unstable or not {expected_configuration}')
        signatures = {(row.get('maximum_bank_angle_deg'),
                       row.get('maximum_load_factor'), row.get('minimum_load_factor'))
                      for row in pair_rows}
        if len(signatures) != 1:
            errors.append(f'{label} duplicated points have inconsistent lateral limits')
        if signatures:
            bank, load, _ = next(iter(signatures))
            summaries.append(f'{label}: config={expected_configuration} '
                             f'bank_max={bank} deg load_max={load}')
    if errors:
        return 'INVALID evidence:\n  - ' + '\n  - '.join(dict.fromkeys(errors))
    return (f'VALID: {len(rows)} samples, BADA 3.15 observations are OFF, event-free, '
            'pair-consistent, and source-derived\n  ' + '\n  '.join(summaries))


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
