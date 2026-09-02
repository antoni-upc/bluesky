import csv
import json

import pytest

from tests.research.validate_era5_tem_run import validate


def _evidence(tmp_path, schema='samples-v10', source='ERA5', family='4'):
    scenario = ('era5-tem-envelope' if source == 'ERA5' and family == '4'
                else f'{source.lower()}-tem-envelope-bada{family}')
    acids = ({'3': ('E3ER', 'E3EO'), '4': ('ERAR', 'ERAO')}[family]
             if source == 'ERA5'
             else {'3': ('G3ER', 'G3EO'), '4': ('G4ER', 'G4EO')}[family])
    aircraft = {'3': 'A320__', '4': 'A320-232'}[family]
    version = {'3': '3.15', '4': '4.2'}[family]
    path = tmp_path / f'{scenario}.csv'
    rows = []
    for acid, policy in zip(acids, ('REPORT', 'OFF')):
        for second in range(1, 17):
            temperature = 280.0
            pressure = 70000.0
            rows.append({
                'sim_time_s': second, 'acid': acid, 'lat_deg': 41.3,
                'lon_deg': 2.1, 'geometric_alt_m': 3048.0,
                'pressure_alt_m': 2900.0, 'tas_m_s': 140.0,
                'cas_m_s': 125.0, 'mach': 0.42, 'vertical_speed_m_s': 0.0,
                'temperature_k': temperature, 'pressure_pa': pressure,
                'density_kg_m3': pressure / (287.05287 * temperature),
                'wind_north_m_s': -5.0, 'wind_east_m_s': 1.0,
                'atmosphere_source': source, 'atmosphere_valid': True,
                'dataset_time': '2025-08-15T12:00:00', 'fallback_reason': '',
                'performance_model': f'PYBADATEM-BADA{family}',
                'performance_dataset_version': version,
                'performance_aircraft': aircraft, 'performance_dummy': False,
                'performance_valid': True, 'performance_miss_count': 0,
                'dynamics_mode': 'TEM', 'thrust_n': 10000.0,
                'maximum_thrust_n': 20000.0, 'idle_thrust_n': 1000.0,
                'drag_n': 10000.0, 'fuel_flow_kg_s': 0.5,
                'mass_kg': 60000.0 - 0.5 * second,
                'applied_acceleration_m_s2': 0.0,
                'applied_vertical_rate_m_s': 0.0,
                'energy_allocation_policy': 'HORIZONTAL_ADAPTED',
                'envelope_policy': policy, 'envelope_profile': 'LONGITUDINAL',
                'envelope_status': 'VALID', 'envelope_failed_checks': '',
                'envelope_event_count': 0,
            })
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    path.with_suffix('.metadata.json').write_text(json.dumps({
        'schema_version': schema, 'scenario': scenario, 'rows': len(rows),
        'atmosphere_sources': [source], 'event_total': 0,
    }), encoding='utf-8')
    path.with_suffix('.events.jsonl').write_text('', encoding='utf-8')
    return path, scenario, acids


@pytest.mark.parametrize('schema', ['samples-v9', 'samples-v10'])
def test_weather_tem_validator_accepts_compatible_schemas(tmp_path, schema):
    path, scenario, acids = _evidence(tmp_path, schema=schema)
    result = validate(path, '4', 'ERA5', scenario, *acids)
    assert result.startswith('VALID:')


@pytest.mark.parametrize(('source', 'family'), [
    ('ERA5', '3'), ('ERA5', '4'), ('GFS', '3'), ('GFS', '4')])
def test_weather_tem_validator_accepts_matrix_cells(tmp_path, source, family):
    path, scenario, acids = _evidence(tmp_path, source=source, family=family)
    result = validate(path, family, source, scenario, *acids)
    assert result.startswith('VALID:')


def test_weather_tem_validator_rejects_incompatible_schema(tmp_path):
    path, scenario, acids = _evidence(tmp_path, schema='samples-v8')
    result = validate(path, '4', 'ERA5', scenario, *acids)
    assert result.startswith('INVALID')
    assert 'not compatible samples-v9/v10' in result


def test_weather_tem_validator_rejects_fallback(tmp_path):
    path, scenario, acids = _evidence(tmp_path)
    rows = list(csv.DictReader(path.open(newline='', encoding='utf-8')))
    rows[0]['fallback_reason'] = 'TIME_SLOT_UNAVAILABLE'
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    result = validate(path, '4', 'ERA5', scenario, *acids)
    assert result.startswith('INVALID')
    assert 'fallback reasons' in result
