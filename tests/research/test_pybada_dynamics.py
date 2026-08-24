from types import SimpleNamespace

import numpy as np
import pytest

import bluesky as bs
from bluesky.plugins.pybada.performance import PyBadaTEM
from bluesky.traffic.dynamics import SpeedStepRequest
from bluesky.traffic.traffic import Traffic


class FakeModel:
    def __init__(self, fail=False, limited=False):
        self.fail = fail
        self.limited = limited

    def bluesky_energy(self, **state):
        if self.fail:
            raise ValueError('injected thrust failure')
        requested = state.get('requested_acceleration', 0.0)
        return dict(thrust=12000.0, rated_thrust=14000.0,
                    drag=10000.0, fuel_flow=0.5,
                    esf=0.5, rocd=5.0, acceleration=requested,
                    idle_thrust=4000.0, maximum_thrust=11000.0,
                    requested_acceleration=requested,
                    applied_acceleration=requested,
                    requested_vertical_rate=state.get('requested_vertical_rate', 0.0),
                    applied_vertical_rate=5.0, allocation_policy='BADA_ESF',
                    propulsion_bank_angle=state.get('propulsion_bank_angle', 0.0),
                    load_factor=state.get('load_factor', 1.0),
                    thrust_limited=self.limited,
                    limitation_reason='ABOVE_MAXIMUM_THRUST' if self.limited else '')


def performance(model, strict=False):
    perf = object.__new__(PyBadaTEM)
    perf.family = '4'
    perf.schedule = 'ICAO'
    perf.strict = strict
    perf.models = [model]
    perf.dyn_mode = np.array([1])
    perf.mass = np.array([60000.0])
    perf.thrust = np.zeros(1)
    perf.rated_thrust = np.zeros(1)
    perf.drag = np.zeros(1)
    perf.fuelflow = np.zeros(1)
    perf.required_thrust = np.zeros(1)
    perf.idle_thrust = np.zeros(1)
    perf.maximum_thrust = np.zeros(1)
    perf.target_tas = np.zeros(1)
    perf.requested_acceleration = np.zeros(1)
    perf.applied_acceleration = np.zeros(1)
    perf.thrust_limited = np.zeros(1, dtype=bool)
    perf.thrust_limitation_reason = np.array([''], dtype='U32')
    perf.speed_capture = np.zeros(1, dtype=bool)
    perf.requested_vertical_rate = np.zeros(1)
    perf.applied_vertical_rate = np.zeros(1)
    perf.energy_share_factor = np.zeros(1)
    perf.energy_allocation_policy = np.array([''], dtype='U24')
    perf.propulsion_bank_angle = np.zeros(1)
    perf.propulsion_load_factor = np.ones(1)
    perf.invalid = np.zeros(1, dtype=bool)
    perf.failure_count = np.zeros(1, dtype=int)
    return perf


def traffic():
    traf = SimpleNamespace(
        ntraf=1, id=['TST1'], type=['A320'], pressure_alt=np.array([5000.0]),
        tas=np.array([200.0]), Temp=np.array([260.0]), p=np.array([54000.0]),
        alt=np.array([5000.0]), ax=np.zeros(1), vs=np.zeros(1),
        hdg=np.array([90.0]), eps=np.array([0.01]),
        ap=SimpleNamespace(turnphi=np.array([0.0]), bankdef=np.radians([25.0])),
        aporasas=SimpleNamespace(alt=np.array([6000.0]), tas=np.array([210.0]),
                                vs=np.array([5.0]), hdg=np.array([90.0])))
    traf.speed_request = SpeedStepRequest(
        target_tas=np.array([210.0]), requested_acceleration=np.array([1.0]),
        capture=np.array([False]), next_tas=np.array([201.0]))
    return traf


@pytest.mark.smoke
def test_tem_updates_once_and_depletes_mass(monkeypatch):
    traf = traffic()
    monkeypatch.setattr(bs, 'traf', traf)
    perf = performance(FakeModel())
    speed_handled, vertical_handled = perf.update_dynamics(traf, 1.0)
    assert speed_handled.tolist() == [True]
    assert vertical_handled.tolist() == [True]
    assert traf.tas[0] == pytest.approx(200.0)
    assert traf.vs[0] == pytest.approx(5.0)
    assert perf.mass[0] == pytest.approx(59999.5)
    assert perf.failure_count[0] == 0


def test_kinematic_computes_performance_without_driving_motion(monkeypatch):
    traf = traffic()
    monkeypatch.setattr(bs, 'traf', traf)
    perf = performance(FakeModel())
    perf.dyn_mode[0] = 0
    speed_handled, vertical_handled = perf.update_dynamics(traf, 1.0)
    assert speed_handled.tolist() == [False]
    assert vertical_handled.tolist() == [False]
    assert traf.tas[0] == pytest.approx(200.0)
    assert traf.vs[0] == pytest.approx(0.0)
    assert perf.thrust[0] == pytest.approx(12000.0)
    assert perf.rated_thrust[0] == pytest.approx(14000.0)
    assert perf.drag[0] == pytest.approx(10000.0)
    assert perf.fuelflow[0] == pytest.approx(0.5)
    assert perf.mass[0] == pytest.approx(59999.5)


def test_current_tick_turn_load_is_passed_without_stale_heading_mask(monkeypatch):
    traf = traffic()
    traf.aporasas.hdg[0] = 180.0
    captured = {}
    model = FakeModel()
    original = model.bluesky_energy
    model.bluesky_energy = lambda **state: (captured.update(state) or original(**state))
    monkeypatch.setattr(bs, 'traf', traf)
    perf = performance(model)
    perf.update_dynamics(traf, 1.0)
    assert captured['propulsion_bank_angle'] == pytest.approx(25.0)
    assert captured['load_factor'] == pytest.approx(1.0 / np.cos(np.radians(25.0)))
    assert perf.propulsion_load_factor[0] == pytest.approx(captured['load_factor'])


def test_straight_flight_uses_exact_unit_load(monkeypatch):
    traf = traffic()
    captured = {}
    model = FakeModel()
    original = model.bluesky_energy
    model.bluesky_energy = lambda **state: (captured.update(state) or original(**state))
    monkeypatch.setattr(bs, 'traf', traf)
    performance(model).update_dynamics(traf, 1.0)
    assert captured['propulsion_bank_angle'] == 0.0
    assert captured['load_factor'] == 1.0


def test_invalid_turn_bank_fails_explicitly(monkeypatch):
    traf = traffic()
    traf.aporasas.hdg[0] = 180.0
    traf.ap.bankdef[0] = np.radians(90.0)
    monkeypatch.setattr(bs, 'traf', traf)
    perf = performance(FakeModel())
    perf.update_dynamics(traf, 1.0)
    assert perf.invalid[0]
    assert perf.failure_count[0] == 1


def test_interactive_failure_is_missing_and_native_fallback(monkeypatch):
    traf = traffic()
    monkeypatch.setattr(bs, 'traf', traf)
    perf = performance(FakeModel(fail=True))
    speed_handled, vertical_handled = perf.update_dynamics(traf, 1.0)
    assert not speed_handled.any() and not vertical_handled.any()
    assert np.isnan(perf.thrust[0]) and np.isnan(perf.drag[0]) and np.isnan(perf.fuelflow[0])
    assert perf.invalid[0] and perf.failure_count[0] == 1


def test_strict_failure_holds_without_terminating_process(monkeypatch):
    traf = traffic()
    monkeypatch.setattr(bs, 'traf', traf)
    held = []
    monkeypatch.setattr(bs, 'sim', SimpleNamespace(hold=lambda: held.append(True)))
    monkeypatch.setattr('bluesky.stack.echo', lambda message: None)
    perf = performance(FakeModel(fail=True), strict=True)
    speed_handled, vertical_handled = perf.update_dynamics(traf, 1.0)
    assert held == [True]
    assert not speed_handled.any() and not vertical_handled.any()
    assert perf.invalid[0] and perf.failure_count[0] == 1
    assert np.isnan(perf.thrust[0]) and np.isnan(perf.drag[0])


def test_strict_thrust_limit_holds_without_terminating_process(monkeypatch):
    traf = traffic()
    monkeypatch.setattr(bs, 'traf', traf)
    held = []
    monkeypatch.setattr(bs, 'sim', SimpleNamespace(hold=lambda: held.append(True)))
    monkeypatch.setattr('bluesky.stack.echo', lambda message: None)
    perf = performance(FakeModel(limited=True), strict=True)
    perf.dyn_mode[0] = 0
    speed_handled, vertical_handled = perf.update_dynamics(traf, 1.0)
    assert held == [True]
    assert not speed_handled.any() and not vertical_handled.any()
    assert perf.invalid[0] and perf.failure_count[0] == 1


@pytest.mark.parametrize(('applied', 'target', 'expected'), [
    (0.25, 210.0, 200.25),
    (-0.4, 190.0, 199.6),
])
def test_tem_applies_thrust_feasible_acceleration(monkeypatch, applied, target, expected):
    traf = traffic()
    traf.aporasas.tas[0] = target
    traf.speed_request = SpeedStepRequest(
        target_tas=np.array([target]), requested_acceleration=np.array([np.sign(applied) * 2.0]),
        capture=np.array([False]), next_tas=np.array([200.0 + np.sign(applied) * 2.0]))
    model = FakeModel(limited=True)
    model.bluesky_energy = lambda **state: dict(
        thrust=11000.0 if applied > 0.0 else 4000.0,
        rated_thrust=14000.0, drag=10000.0, fuel_flow=0.5,
        esf=0.5, rocd=5.0, acceleration=applied,
        requested_acceleration=state['requested_acceleration'],
        applied_acceleration=applied, idle_thrust=4000.0,
        maximum_thrust=11000.0, thrust_limited=True,
        limitation_reason=('ABOVE_MAXIMUM_THRUST' if applied > 0.0
                           else 'BELOW_IDLE_THRUST'))
    monkeypatch.setattr(bs, 'traf', traf)
    perf = performance(model)
    speed_handled, _ = perf.update_dynamics(traf, 1.0)
    assert speed_handled.tolist() == [True]
    assert traf.speed_result.next_tas[0] == pytest.approx(expected)
    assert traf.speed_result.applied_acceleration[0] == pytest.approx(applied)


def test_tem_capture_does_not_overshoot_target(monkeypatch):
    traf = traffic()
    traf.aporasas.tas[0] = 200.1
    traf.speed_request = SpeedStepRequest(
        target_tas=np.array([200.1]), requested_acceleration=np.array([0.1]),
        capture=np.array([True]), next_tas=np.array([200.1]))
    monkeypatch.setattr(bs, 'traf', traf)
    perf = performance(FakeModel())
    perf.update_dynamics(traf, 1.0)
    assert traf.speed_result.capture.tolist() == [True]
    assert traf.speed_result.next_tas[0] == pytest.approx(200.1)
    assert traf.speed_result.applied_acceleration[0] == pytest.approx(0.1)


def test_timestep_convergence_for_constant_reference(monkeypatch):
    final = []
    for dt in (1.0, 0.5, 0.25):
        traf = traffic()
        monkeypatch.setattr(bs, 'traf', traf)
        perf = performance(FakeModel())
        for _ in range(round(4.0 / dt)):
            perf.update_dynamics(traf, dt)
        final.append((traf.tas[0], perf.mass[0]))
    np.testing.assert_allclose(final, np.broadcast_to(final[0], (3, 2)), atol=1e-9)


def test_split_dynamics_uses_native_speed_and_preserves_tem_vertical(monkeypatch):
    monkeypatch.setattr(bs, 'sim', SimpleNamespace(simdt=1.0))
    state = SimpleNamespace(
        ntraf=1, tas=np.array([200.0]), ax=np.array([0.0]),
        perf=SimpleNamespace(axmax=np.array([2.0])),
        aporasas=SimpleNamespace(tas=np.array([210.0]), hdg=np.array([90.0]),
                                alt=np.array([6000.0]), vs=np.array([10.0])),
        ap=SimpleNamespace(turnphi=np.array([0.0]), bankdef=np.array([0.0])),
        eps=np.array([1e-6]), hdg=np.array([90.0]), swhdgsel=np.array([False]),
        alt=np.array([5000.0]), vs=np.array([5.0]), swaltsel=np.array([True]),
        az=np.array([0.0]), _update_airdata=lambda: None)
    Traffic.update_airspeed(
        state, np.array([False]), np.array([True]))
    assert state.tas[0] == pytest.approx(202.0)
    assert state.ax[0] == pytest.approx(2.0)
    assert state.vs[0] == pytest.approx(5.0)


def test_native_speed_request_is_side_effect_free_and_captures_exactly(monkeypatch):
    monkeypatch.setattr(bs, 'sim', SimpleNamespace(simdt=0.5))
    state = SimpleNamespace(
        ntraf=3,
        tas=np.array([100.0, 100.0, 100.0]),
        aporasas=SimpleNamespace(tas=np.array([100.5, 105.0, 95.0])),
        perf=SimpleNamespace(axmax=np.array([2.0, 2.0, 2.0])))
    original = state.tas.copy()
    request = Traffic.native_speed_request(state)
    np.testing.assert_allclose(request.target_tas, [100.5, 105.0, 95.0])
    np.testing.assert_allclose(request.requested_acceleration, [1.0, 2.0, -2.0])
    assert request.capture.tolist() == [True, False, False]
    np.testing.assert_allclose(request.next_tas, [100.5, 101.0, 99.0])
    np.testing.assert_array_equal(state.tas, original)


def test_update_airspeed_consumes_precomputed_request(monkeypatch):
    monkeypatch.setattr(bs, 'sim', SimpleNamespace(simdt=1.0))
    state = SimpleNamespace(
        ntraf=1, tas=np.array([200.0]), ax=np.array([0.0]),
        perf=SimpleNamespace(axmax=np.array([2.0])),
        aporasas=SimpleNamespace(tas=np.array([250.0]), hdg=np.array([90.0]),
                                alt=np.array([5000.0]), vs=np.array([0.0])),
        ap=SimpleNamespace(turnphi=np.array([0.0]), bankdef=np.array([0.0])),
        eps=np.array([1e-6]), hdg=np.array([90.0]), swhdgsel=np.array([False]),
        alt=np.array([5000.0]), vs=np.array([0.0]), swaltsel=np.array([False]),
        az=np.array([0.0]), _update_airdata=lambda: None)
    request = SpeedStepRequest(
        target_tas=np.array([201.0]), requested_acceleration=np.array([1.0]),
        capture=np.array([True]), next_tas=np.array([201.0]))
    Traffic.update_airspeed(state, np.array([False]), np.array([False]), request)
    assert state.tas[0] == pytest.approx(201.0)
    assert state.ax[0] == pytest.approx(1.0)
