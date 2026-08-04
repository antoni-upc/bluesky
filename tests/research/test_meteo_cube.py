from datetime import datetime, timezone

import numpy as np
import pytest

from bluesky.plugins.meteo import GridValidationError, MeteorologyProvider, WeatherCube, previous_slot
from bluesky.tools.aero import R


def cube():
    alt = np.array([0.0, 1000.0])
    lat = np.array([10.0, 11.0])
    lon = np.array([179.0, 181.0])
    z, y, x = np.meshgrid(alt, lat, lon, indexing='ij')
    east = z / 1000.0 + y + x / 100.0
    north = 2.0 * east
    temperature = 280.0 + z / 1000.0 + y / 10.0
    pressure = 100000.0 - 10.0 * z + y + x
    return WeatherCube(alt, lat, lon, east, north, temperature, pressure,
                       'SYNTHETIC', '2026-01-01T00:00:00+00:00')


@pytest.mark.smoke
def test_trilinear_midpoint_and_thermodynamic_consistency():
    weather = cube()
    north, east, sample = weather.interpolate([10.5], [180.0], [500.0])
    assert east[0] == pytest.approx(12.8)
    assert north[0] == pytest.approx(25.6)
    assert sample.valid[0]
    assert sample.density[0] == pytest.approx(sample.pressure[0] / (R * sample.temperature[0]))


def test_boundaries_and_no_extrapolation():
    weather = cube()
    assert weather.interpolate([10.0], [179.0], [0.0])[2].valid[0]
    assert not weather.interpolate([9.999], [179.0], [0.0])[2].valid[0]
    assert not weather.interpolate([10.0], [179.0], [1000.1])[2].valid[0]


def test_antimeridian_requested_domain():
    provider = MeteorologyProvider()
    provider.set_cube(cube(), bounds=(10.0, 179.0, 11.0, -179.0))
    sample = provider.get_atmosphere(np.array([10.5, 10.5]),
                                     np.array([179.5, 0.0]), np.array([500.0, 500.0]), None)
    assert sample.valid.tolist() == [True, False]


def test_grid_rejects_missing_cells_and_duplicate_axis():
    weather = cube()
    bad = weather.temperature.copy()
    bad[0, 0, 0] = np.nan
    with pytest.raises(GridValidationError):
        WeatherCube(weather.altitude, weather.latitude, weather.longitude,
                    weather.east_wind, weather.north_wind, bad, weather.pressure, 'X', 'T')
    with pytest.raises(GridValidationError):
        WeatherCube(weather.altitude, [10.0, 10.0], weather.longitude,
                    weather.east_wind, weather.north_wind, weather.temperature,
                    weather.pressure, 'X', 'T')


def test_previous_slot_boundaries_and_jumps():
    assert previous_slot(datetime(2026, 1, 1, 2, 59, tzinfo=timezone.utc)).hour == 0
    assert previous_slot(datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)).hour == 3
    assert previous_slot(datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc)).day == 1
