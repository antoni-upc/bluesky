import numpy as np
import pytest
from types import SimpleNamespace

import bluesky as bs
from bluesky.tools.aero import R, T0, gamma, p0, vatmos
from bluesky.traffic.atmosphere import AtmosphereSample, mach_to_cas, pressure_altitude, tas_to_mach
from bluesky.traffic.traffic import Traffic
from bluesky.traffic.performance.perfbase import PerfBase
from bluesky.tools.aero import vtas2cas, vtas2mach


@pytest.mark.smoke
def test_actual_atmosphere_conversions():
    tas = np.array([100.0, 250.0])
    temperature = np.array([288.15, 220.0])
    expected_mach = tas / np.sqrt(gamma * R * temperature)
    np.testing.assert_allclose(tas_to_mach(tas, temperature), expected_mach, rtol=1e-13)
    assert pressure_altitude(np.array([p0]))[0] == pytest.approx(0.0, abs=1e-10)
    assert mach_to_cas(np.array([0.0]), np.array([p0]))[0] == pytest.approx(0.0)


def test_pressure_altitude_representative_levels():
    # Independent ISA reference points: sea level and the 11 km tropopause.
    pressures = np.array([101325.0, 22632.06])
    np.testing.assert_allclose(pressure_altitude(pressures), [0.0, 11000.0], atol=0.2)


def test_disabled_provider_preserves_native_airdata_exactly():
    state = type('State', (), {})()
    state.tas = np.array([100.0, 250.0])
    state.alt = np.array([0.0, 11000.0])
    state.Temp = np.array([288.15, 216.65])
    state.p = np.array([101325.0, 22632.06])
    state.atmos_source = ['ISA', 'ISA']
    Traffic._update_airdata(state)
    np.testing.assert_array_equal(state.M, vtas2mach(state.tas, state.alt))
    np.testing.assert_array_equal(state.cas, vtas2cas(state.tas, state.alt))


def test_base_performance_dynamics_hook_handles_no_aircraft():
    traffic = type('TrafficState', (), {'ntraf': 0})()
    handled = PerfBase.update_dynamics(object.__new__(PerfBase), traffic, 0.5)
    assert handled.dtype == np.bool_
    assert handled.shape == (0,)


def test_base_performance_dynamics_hook_leaves_native_traffic_untouched():
    traffic = SimpleNamespace(
        ntraf=2,
        tas=np.array([120.0, 180.0]),
        alt=np.array([1000.0, 3000.0]),
        vs=np.array([0.0, 2.0]),
        ax=np.array([0.0, 0.5]),
    )
    before = {name: value.copy() for name, value in vars(traffic).items()
              if isinstance(value, np.ndarray)}
    handled = PerfBase.update_dynamics(object.__new__(PerfBase), traffic, 0.5)
    np.testing.assert_array_equal(handled, np.zeros(2, dtype=bool))
    for name, expected in before.items():
        np.testing.assert_array_equal(getattr(traffic, name), expected)


def test_atmosphere_synchronizes_exactly_to_current_isa_position(monkeypatch):
    state = SimpleNamespace(
        ntraf=1, alt=np.array([4373.6]), lat=np.array([41.3]), lon=np.array([2.1]),
        tas=np.array([158.5]), p=np.zeros(1), rho=np.zeros(1), Temp=np.zeros(1),
        pressure_alt=np.zeros(1), dtemp=np.zeros(1), atmos_valid=np.zeros(1, dtype=bool),
        atmos_source=['STALE'], atmos_dataset_time=['stale'],
        atmos_fallback_reason=['stale'],
        wind=SimpleNamespace(get_atmosphere=lambda lat, lon, alt, utc: None))
    state._update_airdata = lambda: Traffic._update_airdata(state)
    monkeypatch.setattr(bs, 'sim', SimpleNamespace(utc=None))
    Traffic.update_atmosphere(state)
    expected_p, expected_rho, expected_temp = vatmos(state.alt)
    np.testing.assert_array_equal(state.p, expected_p)
    np.testing.assert_array_equal(state.rho, expected_rho)
    np.testing.assert_array_equal(state.Temp, expected_temp)
    np.testing.assert_array_equal(state.pressure_alt, pressure_altitude(expected_p))
    np.testing.assert_allclose(state.pressure_alt, state.alt, atol=1.0)
    np.testing.assert_array_equal(state.dtemp, np.zeros(1))


def test_provider_atmosphere_resynchronizes_wind_without_counting_work(monkeypatch):
    sample = AtmosphereSample(
        np.array([275.0]), np.array([75000.0]),
        np.array([75000.0 / (R * 275.0)]), np.array([True]),
        'ERA5', '2025-08-15T12:00:00', '')
    calls = []
    state = SimpleNamespace(
        ntraf=1, alt=np.array([2500.0]), lat=np.array([41.3]), lon=np.array([2.1]),
        tas=np.array([140.0]), p=np.zeros(1), rho=np.zeros(1), Temp=np.zeros(1),
        pressure_alt=np.zeros(1), dtemp=np.zeros(1), atmos_valid=np.zeros(1, dtype=bool),
        atmos_source=['ISA'], atmos_dataset_time=[''], atmos_fallback_reason=[''],
        wind=SimpleNamespace(get_atmosphere=lambda lat, lon, alt, utc: sample))
    state._update_airdata = lambda: Traffic._update_airdata(state)
    state.update_groundspeed = lambda accumulate_work=True: calls.append(accumulate_work)
    monkeypatch.setattr(bs, 'sim', SimpleNamespace(utc=None))
    Traffic.update_atmosphere(state)
    assert calls == [False]
    assert state.atmos_source == ['ERA5']
