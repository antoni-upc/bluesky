#!/usr/bin/env python3
"""Validate matched weather/TEM envelope evidence for BADA 3 or BADA 4."""

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

G0 = 9.80665
RD = 287.05287
SUPPORTED_SCHEMAS = {'samples-v9', 'samples-v10'}
NUMERIC_ATMOS = ('temperature_k', 'pressure_pa', 'density_kg_m3',
                 'wind_north_m_s', 'wind_east_m_s', 'pressure_alt_m',
                 'tas_m_s', 'cas_m_s', 'mach')
PAIR_TOLERANCES = {
    'lat_deg': 1e-7, 'lon_deg': 1e-7, 'geometric_alt_m': 0.05,
    'pressure_alt_m': 0.05, 'tas_m_s': 0.02, 'cas_m_s': 0.02,
    'mach': 2e-5, 'vertical_speed_m_s': 0.02, 'temperature_k': 1e-6,
    'pressure_pa': 1e-3, 'density_kg_m3': 1e-8,
    'wind_north_m_s': 1e-6, 'wind_east_m_s': 1e-6,
    'thrust_n': 0.1, 'drag_n': 0.1, 'fuel_flow_kg_s': 1e-6,
    'mass_kg': 1e-4,
}


def _number(row, field):
    value = float(row[field])
    if not math.isfinite(value):
        raise ValueError(f'{row.get("acid", "?")} {field} is non-finite')
    return value


def validate(path, family='4', source='ERA5', scenario=None,
             report_acid=None, off_acid=None, power_tolerance=0.75):
    path = Path(path)
    source = source.upper()
    scenario = scenario or ('era5-tem-envelope' if source == 'ERA5' and family == '4'
                            else f'{source.lower()}-tem-envelope-bada{family}')
    report_acid = report_acid or {'3': 'E3ER', '4': 'ERAR'}[family]
    off_acid = off_acid or {'3': 'E3EO', '4': 'ERAO'}[family]
    expected_aircraft = {'3': 'A320__', '4': 'A320-232'}[family]
    expected_version = {'3': '3.15', '4': '4.2'}[family]
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    metadata = json.loads(path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    event_path = path.with_suffix('.events.jsonl')
    errors = []
    by_acid = defaultdict(list)
    for row in rows:
        by_acid[row.get('acid')].append(row)
    schema = metadata.get('schema_version')
    if schema not in SUPPORTED_SCHEMAS:
        errors.append(f'metadata schema {schema!r} is not compatible samples-v9/v10')
    if metadata.get('scenario') != scenario:
        errors.append(f'metadata scenario is {metadata.get("scenario")!r}, expected {scenario!r}')
    if metadata.get('rows') not in (None, len(rows)):
        errors.append('metadata/CSV row count differs')
    if metadata.get('atmosphere_sources') != [source]:
        errors.append(f'metadata atmosphere sources are not exactly [{source!r}]')
    if metadata.get('event_total') not in (None, 0):
        errors.append('quality events were recorded')
    if event_path.exists() and event_path.read_text(encoding='utf-8').strip():
        errors.append('event ledger is not empty')
    expected_acids = {report_acid, off_acid}
    if set(by_acid) != expected_acids:
        errors.append(f'aircraft are {sorted(by_acid)}, expected {sorted(expected_acids)}')
    max_power_residual = 0.0
    for acid, policy in ((report_acid, 'REPORT'), (off_acid, 'OFF')):
        samples = sorted(by_acid[acid], key=lambda row: _number(row, 'sim_time_s'))
        if len(samples) < 15:
            errors.append(f'{acid} has only {len(samples)} samples')
            continue
        expected = {
            'envelope_policy': policy, 'envelope_profile': 'LONGITUDINAL',
            'dynamics_mode': 'TEM', 'atmosphere_source': source,
            'atmosphere_valid': 'True',
            'performance_model': f'PYBADATEM-BADA{family}',
            'performance_dataset_version': expected_version,
            'performance_aircraft': expected_aircraft,
            'performance_dummy': 'False', 'performance_valid': 'True',
            'performance_miss_count': '0',
        }
        for field, value in expected.items():
            values = {row.get(field) for row in samples}
            if values != {value}:
                errors.append(f'{acid} {field} values are {sorted(values)}, expected {value}')
        if any(not row.get('dataset_time', '').startswith('2025-08-15T12:00:00')
               for row in samples):
            errors.append(f'{acid} has unexpected {source} provenance time')
        if any(row.get('fallback_reason') for row in samples):
            errors.append(f'{acid} contains atmosphere fallback reasons')
        if any(row.get('envelope_status') != 'VALID' for row in samples):
            errors.append(f'{acid} contains a non-VALID envelope state')
        if any(row.get('envelope_failed_checks') for row in samples):
            errors.append(f'{acid} contains failed envelope checks')
        if any(row.get('envelope_event_count') not in ('', '0') for row in samples):
            errors.append(f'{acid} contains envelope events')
        for row in samples:
            values = {field: _number(row, field) for field in NUMERIC_ATMOS}
            if (values['temperature_k'] <= 0 or values['pressure_pa'] <= 0
                    or values['density_kg_m3'] <= 0):
                errors.append(f'{acid} contains non-physical atmosphere values')
                break
            expected_rho = values['pressure_pa'] / (RD * values['temperature_k'])
            if not math.isclose(values['density_kg_m3'], expected_rho, rel_tol=2e-6):
                errors.append(f'{acid} density is inconsistent with pressure and temperature')
                break
            thrust = _number(row, 'thrust_n')
            maximum = _number(row, 'maximum_thrust_n')
            idle_raw = row.get('idle_thrust_n', '')
            if thrust > maximum + 0.2:
                errors.append(f'{acid} thrust exceeds its licensed maximum')
                break
            if idle_raw and thrust < _number(row, 'idle_thrust_n') - 0.2:
                errors.append(f'{acid} thrust is below its licensed idle bound')
                break
        for previous, current in zip(samples, samples[1:]):
            dt = _number(current, 'sim_time_s') - _number(previous, 'sim_time_s')
            if dt <= 0:
                errors.append(f'{acid} timestamps are not strictly increasing')
                continue
            mass_loss = _number(previous, 'mass_kg') - _number(current, 'mass_kg')
            fuel_bounds = sorted((_number(previous, 'fuel_flow_kg_s') * dt,
                                  _number(current, 'fuel_flow_kg_s') * dt))
            if not fuel_bounds[0] - 2e-3 <= mass_loss <= fuel_bounds[1] + 2e-3:
                errors.append(f'{acid} mass/fuel integration mismatch at {current["sim_time_s"]}')
                break
            if current.get('energy_allocation_policy') != 'BADA_ESF':
                continue
            tas = _number(current, 'tas_m_s')
            specific_power = ((_number(current, 'thrust_n') -
                               _number(current, 'drag_n')) * tas /
                              _number(previous, 'mass_kg'))
            isa_temperature = 288.15 - 0.0065 * _number(current, 'pressure_alt_m')
            temperature_factor = isa_temperature / _number(current, 'temperature_k')
            allocated_power = (tas * _number(current, 'applied_acceleration_m_s2') +
                               G0 * _number(current, 'applied_vertical_rate_m_s') /
                               temperature_factor)
            max_power_residual = max(max_power_residual,
                                     abs(specific_power - allocated_power))
    report = {row['sim_time_s']: row for row in by_acid[report_acid]}
    off = {row['sim_time_s']: row for row in by_acid[off_acid]}
    common = sorted(set(report).intersection(off), key=float)
    if len(common) < 15:
        errors.append(f'only {len(common)} time-aligned samples')
    maxima = {}
    for field, tolerance in PAIR_TOLERANCES.items():
        differences = [abs(_number(report[t], field) - _number(off[t], field))
                       for t in common]
        if differences:
            maxima[field] = max(differences)
            if maxima[field] > tolerance:
                errors.append(f'REPORT/OFF {field} difference {maxima[field]:.6g} exceeds {tolerance}')
    if max_power_residual > power_tolerance:
        errors.append(f'maximum total-energy residual {max_power_residual:.6f} W/kg')
    if errors:
        return 'INVALID weather/TEM envelope evidence:\n  - ' + '\n  - '.join(errors)
    return (f'VALID: {len(rows)} {source}/BADA {family} TEM envelope samples; '
            f'REPORT/OFF matched, max pair delta={max(maxima.values(), default=0.0):.6g}, '
            f'max power residual={max_power_residual:.6f} W/kg')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv')
    parser.add_argument('--family', choices=('3', '4'), default='4')
    parser.add_argument('--source', choices=('ERA5', 'GFS'), default='ERA5')
    parser.add_argument('--scenario')
    parser.add_argument('--report-acid')
    parser.add_argument('--off-acid')
    args = parser.parse_args(argv)
    try:
        result = validate(args.csv, args.family, args.source, args.scenario,
                          args.report_acid, args.off_acid)
    except (OSError, ValueError, KeyError, json.JSONDecodeError,
            ZeroDivisionError) as exc:
        result = ('INVALID weather/TEM envelope evidence:\n  - '
                  f'evidence could not be validated: {exc}')
    print(result)
    return int(result.startswith('INVALID'))


if __name__ == '__main__':
    raise SystemExit(main())
