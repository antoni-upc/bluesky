import csv
import json

from tests.research.validate_horizontal_acceleration_run import validate


FIELDS = (
    'schema_version', 'sim_time_s', 'acid', 'tas_m_s', 'vertical_speed_m_s',
    'performance_model', 'performance_aircraft', 'performance_dummy',
    'performance_valid', 'performance_miss_count', 'thrust_n', 'drag_n',
    'fuel_flow_kg_s', 'mass_kg')


def _evidence(tmp_path, force_error=0.0):
    path = tmp_path / 'pybada-acceleration-bada4.csv'
    accelerations = ([0.0] * 4 + [0.1] * 11 + [-0.1] * 11 + [0.0] * 4)
    rows = []
    for acid in ('B4AK', 'B4AT'):
        tas, mass = 120.0, 60000.0
        for second, acceleration in enumerate(accelerations, 1):
            previous_mass = mass
            fuel = 0.5 + 0.1 * acceleration
            mass -= fuel
            tas += acceleration
            drag = 10000.0
            thrust = drag + previous_mass * acceleration
            if force_error and acid == 'B4AK' and second == 5:
                thrust += previous_mass * force_error
            rows.append({
                'schema_version': 'samples-v7', 'sim_time_s': second,
                'acid': acid, 'tas_m_s': tas, 'vertical_speed_m_s': 0.0,
                'performance_model': 'PYBADATEM-BADA4',
                'performance_aircraft': 'A320-232', 'performance_dummy': False,
                'performance_valid': True, 'performance_miss_count': 0,
                'thrust_n': thrust, 'drag_n': drag,
                'fuel_flow_kg_s': fuel, 'mass_kg': mass})
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    path.with_suffix('.metadata.json').write_text(json.dumps({
        'schema_version': 'samples-v7',
        'scenario': 'pybada-acceleration-bada4',
        'sample_intervals_s': [1.0]}), encoding='utf-8')
    return path


def test_horizontal_acceleration_validator_accepts_balanced_evidence(tmp_path):
    result = validate(_evidence(tmp_path), '4')
    assert result.startswith('VALID:')


def test_horizontal_acceleration_validator_rejects_force_mismatch(tmp_path):
    result = validate(_evidence(tmp_path, force_error=0.2), '4')
    assert result.startswith('INVALID evidence:')
    assert 'force-balance mismatch' in result
