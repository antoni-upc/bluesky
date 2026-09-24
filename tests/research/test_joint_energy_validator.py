"""Portable contract checks for the applied joint-energy policy selector."""

import csv
import json

import pytest

from tests.research.schema_compat import SCHEMA_VERSION
from tests.research.validate_joint_energy_run import G0, validate


def evidence(tmp_path, family):
    path = tmp_path / 'joint.csv'
    rows = []
    for step in range(501):
        t = step * 0.05
        moving = 0 < step < 500
        tas = 130.0 + 0.5 * min(t, 499 * 0.05)
        altitude = 3607.7 + 2.0 * min(t, 499 * 0.05)
        previous_mass = 60000.0 - 0.5 * max(t - 0.05, 0.0)
        acceleration = 0.5 if moving else 0.0
        vertical = 2.0 if moving else 0.0
        rows.append({
            'acid': f'B{family}JE', 'sim_time_s': t,
            'performance_model': f'PYBADATEM-BADA{family}',
            'performance_dataset_version': '3.15' if family == '3' else '4.2',
            'performance_aircraft': 'A320__' if family == '3' else 'A320-232',
            'performance_dummy': 'False', 'performance_valid': 'True',
            'performance_miss_count': '0', 'dynamics_mode': 'TEM',
            'energy_allocation_policy': 'SPEED_PRIORITY',
            'energy_share_factor': 0.8,
            'applied_acceleration_m_s2': acceleration,
            'applied_vertical_rate_m_s': vertical,
            'tas_m_s': tas, 'geometric_alt_m': altitude,
            'pressure_alt_m': 3000, 'temperature_k': 288.15 - 0.0065 * 3000,
            'mass_kg': 60000.0 - 0.5 * t, 'fuel_flow_kg_s': 0.5,
            'drag_n': 30000.0,
            'thrust_n': 30000.0 + previous_mass * (acceleration + G0 * vertical / tas),
            'idle_thrust_n': 10000.0, 'maximum_thrust_n': 100000.0,
            'vertical_speed_m_s': vertical, 'speed_capture': str(not moving),
        })
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    path.with_suffix('.metadata.json').write_text(json.dumps({
        'schema_version': SCHEMA_VERSION,
        'scenario': f'pybada-joint-energy-bada{family}',
        'sample_intervals_s': [0.05], 'event_total': 0,
    }), encoding='utf-8')
    return path, rows


@pytest.mark.parametrize('family', ['3', '4'])
def test_joint_energy_accepts_applied_speed_priority_and_rejects_old_label(
        tmp_path, family):
    path, rows = evidence(tmp_path, family)
    assert validate(path, family).startswith('VALID:')
    for row in rows:
        row['energy_allocation_policy'] = 'BADA_ESF'
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    assert 'only 0 joint SPEED_PRIORITY samples' in validate(path, family)


def test_joint_energy_rejects_unbalanced_applied_thrust(tmp_path):
    path, rows = evidence(tmp_path, '3')
    rows[100]['thrust_n'] += 20000.0
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    assert 'maximum total-energy residual' in validate(path, '3')
