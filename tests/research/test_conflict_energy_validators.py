"""Portable regression evidence for below-idle requests and timestep gates."""

import csv
import json

import pytest

from tests.research.schema_compat import SCHEMA_VERSION
from tests.research.validate_conflict_energy_run import validate as validate_conflict
from tests.research.validate_timestep_convergence_run import validate as validate_convergence


G = 9.80665


def make_evidence(tmp_path, family, *, label=None):
    cadence = 0.05 if label is None else 0.1
    count = round(120.0 / cadence) + 1
    climb_end = round(100.0 / cadence)
    stem = (f'pybada-conflict-energy-bada{family}' if label is None else
            f'pybada-convergence-bada{family}-{label}')
    path = tmp_path / f'{stem}.csv'
    rows = []
    tas, altitude = 200.0, 3557.6
    for step in range(count):
        t = round(step * cadence, 8)
        old_tas, old_altitude = tas, altitude
        old_mass = 60000.0 - 0.5 * max(t - cadence, 0.0)
        conflict = 5.0 < t <= 7.0
        if step == 0 or step > climb_end:
            vertical, acceleration = 0.0, 0.0
        elif conflict:
            acceleration = (-20000.0 / old_mass - G / old_tas)
            tas = old_tas + acceleration * cadence
            vertical = (-20000.0 / old_mass - acceleration) * tas / G
        else:
            vertical = (3657.6 - altitude) / ((climb_end - step + 1) * cadence)
            acceleration = 0.0
        if not conflict:
            tas = old_tas
        altitude = old_altitude + vertical * cadence
        thrust = (10000.0 if conflict else
                  30000.0 + old_mass * (acceleration + G * vertical / tas))
        rows.append({
            'acid': f'B{family}' + ('CE' if label is None else 'CV'),
            'sim_time_s': t, 'performance_model': f'PYBADATEM-BADA{family}',
            'performance_dataset_version': '3.15' if family == '3' else '4.2',
            'performance_aircraft': 'A320__' if family == '3' else 'A320-232',
            'performance_dummy': 'False', 'performance_valid': 'True',
            'performance_miss_count': '0', 'dynamics_mode': 'TEM',
            'energy_allocation_policy': 'SPEED_PRIORITY',
            'energy_share_factor': 0.8,
            'requested_acceleration_m_s2': -1.0 if conflict else 0.0,
            'requested_vertical_rate_m_s': 5.0 if conflict else vertical,
            'applied_acceleration_m_s2': acceleration,
            'applied_vertical_rate_m_s': vertical,
            'tas_m_s': tas, 'target_tas_m_s': 190.0 if conflict else tas,
            'geometric_alt_m': altitude, 'vertical_speed_m_s': vertical,
            'pressure_alt_m': 3000.0,
            'temperature_k': 288.15 - 0.0065 * 3000.0,
            'mass_kg': 60000.0 - 0.5 * t, 'fuel_flow_kg_s': 0.5,
            'drag_n': 30000.0, 'thrust_n': thrust,
            'idle_thrust_n': 10000.0, 'maximum_thrust_n': 100000.0,
            'speed_capture': str(not conflict),
        })
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    path.with_suffix('.metadata.json').write_text(json.dumps({
        'schema_version': SCHEMA_VERSION, 'scenario': stem,
        'sample_intervals_s': [cadence], 'base_timestep_s': (
            None if label is None else {'dt100': .1, 'dt050': .05, 'dt020': .02}[label]),
        'event_total': 0, 'columns': list(rows[0]),
    }), encoding='utf-8')
    return path, rows


@pytest.mark.parametrize('family', ['3', '4'])
def test_conflict_gate_accepts_idle_deceleration_and_rejects_old_policy(tmp_path, family):
    path, rows = make_evidence(tmp_path, family)
    assert validate_conflict(path, family).startswith('VALID:')
    for row in rows:
        row['energy_allocation_policy'] = 'BADA_ESF'
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    assert 'insufficient conflicting-command evidence: 0' in validate_conflict(path, family)


@pytest.mark.parametrize('family', ['3', '4'])
def test_convergence_gate_compares_complete_applied_trajectories(tmp_path, family):
    paths = {label: make_evidence(tmp_path, family, label=label)[0]
             for label in ('dt100', 'dt050', 'dt020')}
    assert validate_convergence(paths, family).startswith('VALID:')
