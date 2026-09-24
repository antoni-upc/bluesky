import numpy as np
import pytest
from types import SimpleNamespace

import bluesky as bs
from bluesky.tools.aero import R, T0, gamma, p0, vatmos
from bluesky.traffic.atmosphere import (AtmosphereSample, cas_to_mach, casormach_to_tas, mach_to_cas,
                                        pressure_altitude, tas_to_mach)
from bluesky.traffic.traffic import Traffic
from bluesky.tools.aero import kts, vcasormach2tas, vtas2cas, vtas2mach


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


def test_provider_atmosphere_preserves_vectorized_provenance(monkeypatch):
    sample = AtmosphereSample(
        np.array([288.15, 275.0]), np.array([101325.0, 75000.0]),
        np.array([101325.0 / (R * 288.15), 75000.0 / (R * 275.0)]),
        np.array([True, True]), np.array(['ISA', 'ERA5'], dtype=object),
        np.array(['', '2025-08-15T12:00:00'], dtype=object),
        np.array(['CONFIGURED_BELOW_ERA5_DOMAIN', ''], dtype=object))
    state = SimpleNamespace(
        ntraf=2, alt=np.array([0.0, 2500.0]), lat=np.array([41.3, 41.4]),
        lon=np.array([2.1, 2.2]), tas=np.array([100.0, 140.0]), p=np.zeros(2),
        rho=np.zeros(2), Temp=np.zeros(2), pressure_alt=np.zeros(2),
        dtemp=np.zeros(2), atmos_valid=np.zeros(2, dtype=bool),
        atmos_source=['', ''], atmos_dataset_time=['', ''],
        atmos_fallback_reason=['', ''],
        wind=SimpleNamespace(get_atmosphere=lambda lat, lon, alt, utc: sample))
    state._update_airdata = lambda: Traffic._update_airdata(state)
    state.update_groundspeed = lambda accumulate_work=True: None
    monkeypatch.setattr(bs, 'sim', SimpleNamespace(utc=None))
    Traffic.update_atmosphere(state)
    assert state.atmos_source == ['ISA', 'ERA5']
    assert state.atmos_dataset_time == ['', '2025-08-15T12:00:00']
    assert state.atmos_fallback_reason == ['CONFIGURED_BELOW_ERA5_DOMAIN', '']


def test_cas_to_mach_inverts_mach_to_cas():
    mach = np.array([0.2, 0.5, 0.78, 0.9])
    pressure = np.array([95000.0, 50000.0, 23000.0, 19000.0])
    np.testing.assert_allclose(cas_to_mach(mach_to_cas(mach, pressure), pressure), mach,
                               rtol=1e-12)


def test_applied_conversion_matches_native_isa_conversion_in_isa_air():
    speed = np.array([250.0 * kts, 0.78, 310.0 * kts])
    alt = np.array([3000.0, 11000.0, 7000.0])
    pressure, _, temperature = vatmos(alt)
    # Agreement to BlueSky's rounded aero constants; ISA traffic keeps the
    # native functions themselves, so the native path stays exact.
    np.testing.assert_allclose(casormach_to_tas(speed, temperature, pressure),
                               vcasormach2tas(speed, alt), rtol=1e-8)


def weather_state(sources, temperature, pressure):
    n = len(sources)
    state = SimpleNamespace(
        atmos_source=list(sources), alt=np.full(n, 10000.0), Temp=np.asarray(temperature),
        p=np.asarray(pressure), tas=np.zeros(n), cas=np.zeros(n), M=np.zeros(n),
        selspd=np.zeros(n), groundspeed_updates=[])
    state._update_airdata = lambda: Traffic._update_airdata(state)
    state.applied_tas = lambda *args, **kwargs: Traffic.applied_tas(state, *args, **kwargs)
    state.update_groundspeed = lambda accumulate_work=True: \
        state.groundspeed_updates.append(accumulate_work)
    return state


def test_applied_tas_keeps_the_native_result_without_weather():
    state = weather_state(['ISA', 'ISA'], [250.0, 250.0], [30000.0, 30000.0])
    native = vcasormach2tas(np.array([0.78, 128.6]), state.alt)
    assert Traffic.applied_tas(state, native, np.array([0.78, 128.6])) is native


def test_applied_tas_flies_the_selected_cas_or_mach_in_weather():
    # ISA+10 K air; the ISA aircraft and a negative speed keep native values.
    temperature, pressure = np.full(4, 233.2), np.full(4, 26500.0)
    state = weather_state(['ISA', 'ERA5', 'ERA5', 'ERA5'], temperature, pressure)
    speed = np.array([0.78, 0.78, 128.6, -1.0])
    native = vcasormach2tas(speed, state.alt)
    tas = Traffic.applied_tas(state, native, speed)
    assert tas[0] == native[0] and tas[3] == native[3]
    assert tas_to_mach(tas[1], temperature[1]) == pytest.approx(0.78, rel=1e-12)
    assert mach_to_cas(tas_to_mach(tas[2], temperature[2]), pressure[2]) == \
        pytest.approx(128.6, rel=1e-12)
    # Turn speeds are CAS only: 0.78 is a 0.78 m/s CAS, not Mach 0.78 (~240 m/s TAS).
    cas_only = Traffic.applied_tas(state, native, speed, mach=False)
    assert cas_only[1] < 5.0 and cas_only[2] == tas[2]


def test_commanded_airspeed_uses_the_applied_atmosphere():
    state = weather_state(['ERA5', 'ERA5', 'ISA'], [233.2] * 3, [26500.0] * 3)
    command = np.array([128.6, 0.78, 0.78])
    state.tas[:], _, _ = (np.asarray(v) for v in __import__('bluesky.tools.aero', fromlist=['x'])
                          .vcasormach(command, state.alt))
    native_isa = state.tas[2]
    Traffic._command_airspeed(state, slice(0, 3), command)
    assert state.cas[0] == pytest.approx(128.6, rel=1e-12)
    assert state.M[1] == pytest.approx(0.78, rel=1e-12)
    assert state.tas[2] == native_isa
    np.testing.assert_array_equal(state.selspd, state.cas)
    assert state.groundspeed_updates == [False]


def test_commanded_airspeed_leaves_isa_traffic_untouched():
    state = weather_state(['ISA'], [250.0], [30000.0])
    state.tas[:] = 200.0
    Traffic._command_airspeed(state, 0, 0.78)
    assert state.tas[0] == 200.0 and state.groundspeed_updates == []

