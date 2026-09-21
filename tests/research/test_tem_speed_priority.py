"""Regression tests for persistent speed error and truthful envelope status."""
import json
from pathlib import Path

import numpy as np
import pytest
import bluesky as bs
from tests.research.test_numerical_corrections import configure, assert_balance
from tests.research.test_pybada_envelope import traffic, make_perf, FakeFlightModel
from bluesky.plugins.pybada.envelope import EnvelopeCheck


def test_climbing_aircraft_decelerates_towards_distant_lower_target(monkeypatch):
    traf, model, perf = configure(monkeypatch, target=180.)
    traf.speed_request.requested_acceleration[:] = -2.
    perf.update_dynamics(traf, .5)
    assert traf.speed_result.next_tas[0] < 200.
    assert 0 <= perf.thrust[0] <= model.maximum
    assert_balance(traf, perf)
    assert model.last_fuel_thrust == pytest.approx(perf.thrust[0])


def test_acceleration_takes_power_from_climb_before_speed_is_sacrificed(monkeypatch):
    traf, model, perf = configure(monkeypatch, target=220.)
    model.maximum = 70000.
    traf.speed_request.requested_acceleration[:] = .8
    perf.update_dynamics(traf, .5)
    assert traf.speed_result.applied_acceleration[0] == pytest.approx(.8)
    assert 0 < traf.vs[0] < 16.
    assert_balance(traf, perf)


def test_enforce_does_not_report_current_overspeed_as_valid(monkeypatch):
    traffic(monkeypatch)
    monkeypatch.setattr('bluesky.stack.echo', lambda message: None)
    perf = make_perf(('ENFORCE', 'OFF'))
    perf.models = [FakeFlightModel(), FakeFlightModel()]
    perf.envelope_checks[0] = (EnvelopeCheck.HIGH_SPEED,)
    bs.traf.cas[0] = 250.
    bs.traf.tas[0] = 250.
    perf.limits(np.array([180., 120.]), np.zeros(2),
                np.array([3000., 3000.]), np.zeros(2))
    assert perf.envelope_status[0] == 'INFEASIBLE'
    assert EnvelopeCheck.HIGH_SPEED in perf.envelope_state_failed_checks[0]


@pytest.mark.parametrize('case', json.loads(
    (Path(__file__).parent / 'data/tem-speed-regression-states.json').read_text()),
    ids=lambda case: case['route'])
def test_observed_overspeed_states_produce_recovery_acceleration(case):
    from bluesky.plugins.pybada.model import allocate_speed_priority
    from bluesky.tools.aero import g0
    v = case['values']
    tas, mass = v['evaluation_tas_m_s'], v['evaluation_mass_kg']
    nominal_w = v['applied_vertical_rate_m_s']
    thrust, acceleration, vertical, _, _, _ = allocate_speed_priority(
        tas=tas, mass=mass, drag=v['drag_n'], idle_thrust=v['idle_thrust_n'],
        maximum_thrust=v['maximum_thrust_n'],
        requested_acceleration=v['requested_acceleration_m_s2'],
        preferred_vertical_rate=nominal_w,
        minimum_vertical_rate=min(0., nominal_w), maximum_vertical_rate=max(0., nominal_w))
    assert tas > v['target_tas_m_s']
    assert acceleration < -.01
    assert acceleration < v['applied_acceleration_m_s2']
    assert v['idle_thrust_n'] <= thrust <= v['maximum_thrust_n']
    assert (thrust-v['drag_n'])*tas/mass == pytest.approx(tas*acceleration+g0*vertical, abs=1e-9)


@pytest.mark.parametrize('vertical', [-15., 0., 15.])
@pytest.mark.parametrize('requested', [-3., -.1, 0., .1, 3.])
def test_allocation_remains_inside_force_and_vertical_limits(vertical, requested):
    from bluesky.plugins.pybada.model import allocate_speed_priority
    from bluesky.tools.aero import g0
    t, a, w, _, _, _ = allocate_speed_priority(
        tas=200., mass=60000., drag=30000., idle_thrust=10000., maximum_thrust=80000.,
        requested_acceleration=requested, preferred_vertical_rate=vertical,
        minimum_vertical_rate=min(0., vertical), maximum_vertical_rate=max(0., vertical))
    assert 10000. <= t <= 80000.
    assert min(0., vertical)-1e-10 <= w <= max(0., vertical)+1e-10
    assert (t-30000.)*200/60000 == pytest.approx(200*a+g0*w, abs=1e-9)
    if requested != 0:
        assert a*requested > 0


def test_descent_rate_is_reduced_to_allow_deceleration_at_idle():
    from bluesky.plugins.pybada.model import allocate_speed_priority
    from bluesky.tools.aero import g0
    t, a, w, _, _, _ = allocate_speed_priority(
        tas=200., mass=60000., drag=30000., idle_thrust=10000., maximum_thrust=80000.,
        requested_acceleration=-.2, preferred_vertical_rate=-15.,
        minimum_vertical_rate=-15., maximum_vertical_rate=0.)
    assert a == pytest.approx(-.2)
    assert -15. < w < 0.
    assert (t-30000.)*200/60000 == pytest.approx(200*a+g0*w, abs=1e-9)


def test_no_available_deceleration_does_not_fabricate_target_capture(monkeypatch):
    traf, model, perf = configure(monkeypatch, target=180., delta_alt=0., idle=30000.)
    traf.speed_request.requested_acceleration[:] = -2.
    perf.update_dynamics(traf, .5)
    assert traf.speed_result.next_tas[0] > 200.
    assert not traf.speed_result.capture[0]
    assert perf.thrust_limitation_reason[0] == 'BELOW_IDLE_THRUST'
    assert_balance(traf, perf)


def test_current_state_finding_clears_only_after_speed_recovers(monkeypatch):
    traffic(monkeypatch)
    monkeypatch.setattr('bluesky.stack.echo', lambda message: None)
    perf = make_perf(('ENFORCE', 'OFF'))
    perf.models = [FakeFlightModel(), FakeFlightModel()]
    perf.envelope_checks[0] = (EnvelopeCheck.HIGH_SPEED,)
    for actual, expected in [(250., 'INFEASIBLE'), (230., 'INFEASIBLE'), (180., 'VALID')]:
        bs.traf.cas[0] = bs.traf.tas[0] = actual
        perf.limits(np.array([180., 120.]), np.zeros(2), np.array([3000., 3000.]), np.zeros(2))
        assert perf.envelope_status[0] == expected
