from types import SimpleNamespace

import numpy as np
import pytest

import bluesky as bs
from bluesky.plugins.pybada.performance import PyBadaTEM
from bluesky.traffic.dynamics import SpeedStepRequest
from bluesky.traffic.traffic import Traffic


class FakeModel:
    def __init__(self, fail=False):
        self.fail = fail

    def bluesky_energy(self, **state):
        if self.fail:
            raise ValueError('injected thrust failure')
        return dict(thrust=12000.0, rated_thrust=14000.0,
                    drag=10000.0, fuel_flow=0.5,
                    esf=0.5, rocd=5.0, acceleration=0.2)


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
    perf.invalid = np.zeros(1, dtype=bool)
    perf.failure_count = np.zeros(1, dtype=int)
    return perf


def traffic():
    return SimpleNamespace(
        ntraf=1, id=['TST1'], type=['A320'], pressure_alt=np.array([5000.0]),
        tas=np.array([200.0]), Temp=np.array([260.0]), p=np.array([54000.0]),
        alt=np.array([5000.0]), ax=np.zeros(1), vs=np.zeros(1),
        aporasas=SimpleNamespace(alt=np.array([6000.0])))


@pytest.mark.smoke
def test_tem_updates_once_and_depletes_mass(monkeypatch):
    traf = traffic()
    monkeypatch.setattr(bs, 'traf', traf)
    perf = performance(FakeModel())
    speed_handled, vertical_handled = perf.update_dynamics(traf, 1.0)
    assert speed_handled.tolist() == [False]
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


def test_interactive_failure_is_missing_and_native_fallback(monkeypatch):
    traf = traffic()
    monkeypatch.setattr(bs, 'traf', traf)
    perf = performance(FakeModel(fail=True))
    speed_handled, vertical_handled = perf.update_dynamics(traf, 1.0)
    assert not speed_handled.any() and not vertical_handled.any()
    assert np.isnan(perf.thrust[0]) and np.isnan(perf.drag[0]) and np.isnan(perf.fuelflow[0])
    assert perf.invalid[0] and perf.failure_count[0] == 1


def test_strict_failure_aborts_with_context(monkeypatch):
    traf = traffic()
    monkeypatch.setattr(bs, 'traf', traf)
    perf = performance(FakeModel(fail=True), strict=True)
    with pytest.raises(RuntimeError, match='TST1/A320'):
        perf.update_dynamics(traf, 1.0)


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
