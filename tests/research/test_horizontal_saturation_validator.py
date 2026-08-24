import csv
import json

from tests.research.validate_horizontal_saturation_run import validate


FIELDS = (
    'schema_version', 'sim_time_s', 'acid', 'tas_m_s', 'target_tas_m_s',
    'vertical_speed_m_s', 'performance_model', 'performance_aircraft',
    'performance_dummy', 'performance_valid', 'performance_miss_count',
    'dynamics_mode', 'thrust_n', 'required_thrust_n', 'rated_thrust_n',
    'idle_thrust_n', 'maximum_thrust_n', 'drag_n', 'fuel_flow_kg_s',
    'mass_kg', 'requested_acceleration_m_s2', 'applied_acceleration_m_s2',
    'thrust_limited', 'thrust_limitation_reason', 'speed_capture')


def _evidence(tmp_path, force_error=0.0):
    path = tmp_path / 'pybada-saturation-bada4.csv'
    rows = []
    dt = 0.05
    for acid, mode in (('B4ST', 'TEM'),):
        tas, mass, target = 100.0, 60000.0, 100.0
        for index in range(180):
            if index == 10:
                target = 101.0
            if index == 80:
                target = 100.0
            delta = target - tas
            requested = 0.0 if abs(delta) < 1e-12 else (2.0 if delta > 0.0 else -2.0)
            feasible = 0.5 if requested > 0.0 else (-0.4 if requested < 0.0 else 0.0)
            observed = feasible
            if abs(observed * dt) >= abs(delta):
                observed = delta / dt
            limited = requested != 0.0 and abs(feasible) < abs(requested)
            tas += observed * dt
            drag = 10000.0
            thrust = drag + 60000.0 * feasible
            if force_error and acid == 'B4ST' and index == 10:
                thrust += 60000.0 * force_error
            required = drag + 60000.0 * requested
            idle, maximum = 10000.0 - 60000.0 * 0.4, 10000.0 + 60000.0 * 0.5
            fuel = 0.5 + max(feasible, 0.0) * 0.1
            mass -= fuel * dt
            rows.append({
                'schema_version': 'samples-v9', 'sim_time_s': (index + 1) * dt,
                'acid': acid, 'tas_m_s': tas, 'target_tas_m_s': target,
                'vertical_speed_m_s': 0.0, 'performance_model': 'PYBADATEM-BADA4',
                'performance_aircraft': 'A320-232', 'performance_dummy': False,
                'performance_valid': True, 'performance_miss_count': 0,
                'dynamics_mode': mode, 'thrust_n': thrust,
                'required_thrust_n': required, 'rated_thrust_n': maximum,
                'idle_thrust_n': idle, 'maximum_thrust_n': maximum,
                'drag_n': drag, 'fuel_flow_kg_s': fuel, 'mass_kg': mass,
                'requested_acceleration_m_s2': requested,
                'applied_acceleration_m_s2': feasible,
                'thrust_limited': limited,
                'thrust_limitation_reason': ('ABOVE_MAXIMUM_THRUST' if requested > 0.0
                                             else ('BELOW_IDLE_THRUST'
                                                   if requested < 0.0 else '')),
                'speed_capture': abs(target - tas) < 1e-12})
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    path.with_suffix('.metadata.json').write_text(json.dumps({
        'schema_version': 'samples-v9', 'scenario': 'pybada-saturation-bada4',
        'sample_intervals_s': [0.05], 'columns': list(FIELDS),
        'event_total': 0}), encoding='utf-8')
    return path


def test_saturation_validator_accepts_applied_motion_and_capture(tmp_path):
    assert validate(_evidence(tmp_path), '4').startswith('VALID:')


def test_saturation_validator_rejects_applied_force_mismatch(tmp_path):
    result = validate(_evidence(tmp_path, force_error=0.2), '4')
    assert result.startswith('INVALID evidence:')
    assert 'applied-motion mismatch' in result
