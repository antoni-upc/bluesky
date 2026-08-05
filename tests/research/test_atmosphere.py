import numpy as np
import pytest

from bluesky.tools.aero import R, T0, gamma, p0
from bluesky.traffic.atmosphere import mach_to_cas, pressure_altitude, tas_to_mach
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
