from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

import bluesky as bs
from bluesky.plugins.meteo import GridValidationError, MeteorologyProvider, WeatherCube, previous_slot
from bluesky.plugins.windecmwf import WindECMWF
from bluesky.plugins.windgfs import WindGFS
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


def test_provider_specific_analysis_cycle():
    class SixHourlyProvider(MeteorologyProvider):
        slot_hours = 6

    provider = SixHourlyProvider()
    utc = datetime(2026, 1, 1, 5, 59, tzinfo=timezone.utc)
    assert provider.desired_slot(utc).hour == 0
    assert provider.desired_slot(utc.replace(hour=6)).hour == 6


def test_disabled_time_update_expires_weather_with_explicit_reason(monkeypatch):
    provider = MeteorologyProvider()
    provider.set_cube(cube(), bounds=(10.0, 179.0, 11.0, -179.0))
    monkeypatch.setattr(provider, 'strict', False)
    provider.expire_time_slot(datetime(2026, 1, 1, 1, tzinfo=timezone.utc))
    sample = provider.get_atmosphere([10.5], [179.5], [500.0], None)
    assert not sample.valid[0]
    assert sample.fallback_reason.startswith('TIME_SLOT_EXPIRED:')


def test_strict_provider_rejects_outside_requested_bounds(monkeypatch):
    provider = MeteorologyProvider()
    provider.set_cube(cube(), bounds=(10.0, 179.0, 11.0, -179.0))
    monkeypatch.setattr(provider, 'strict', True)
    with pytest.raises(RuntimeError, match='validated domain'):
        provider.get_atmosphere([10.5], [0.0], [500.0], None)


def test_failed_time_update_discards_stale_cube_and_reports_fallback(monkeypatch):
    provider = MeteorologyProvider()
    provider.set_cube(cube(), bounds=(10.0, 179.0, 11.0, -179.0))
    monkeypatch.setattr(provider, 'strict', False)
    slot = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
    success, reason = provider.advance_time_slot(
        slot, lambda: (_ for _ in ()).throw(OSError('download failed')))
    assert not success
    assert reason.startswith('TIME_SLOT_UNAVAILABLE:')
    assert provider.cube is None
    sample = provider.get_atmosphere([10.5], [179.5], [500.0], None)
    assert not sample.valid[0]
    assert sample.fallback_reason == reason


def test_failed_time_update_stops_strict_provider(monkeypatch):
    provider = MeteorologyProvider()
    provider.set_cube(cube(), bounds=(10.0, 179.0, 11.0, -179.0))
    monkeypatch.setattr(provider, 'strict', True)
    slot = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(RuntimeError, match='no valid weather'):
        provider.advance_time_slot(
            slot, lambda: (_ for _ in ()).throw(OSError('download failed')))
    assert provider.cube is None


def test_point_status_reports_loaded_weather():
    provider = MeteorologyProvider()
    provider.set_cube(cube(), bounds=(10.0, 179.0, 11.0, -179.0))
    success, message = provider.point_status(10.5, 179.5, 500.0)
    assert success
    assert all(field in message for field in
               ('lat=10.500000 deg', 'lon=179.500000 deg', 'alt=500.0 m/1640.4 ft',
                'valid=True', 'wind_north=', 'wind_east=', 'T=', 'p=', 'rho='))


def test_slot_changes_synchronously_at_exact_utc_boundary(monkeypatch):
    provider = MeteorologyProvider()
    provider.slot_hours = 1
    start = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    provider.request_bounds = (10.0, 179.0, 11.0, -179.0)
    provider.set_cube(cube(), provider.request_bounds, start)
    loaded = []

    def load(*bounds, slot=None):
        loaded.append(slot)
        replacement = cube()
        replacement.dataset_time = slot.isoformat()
        provider.set_cube(replacement, bounds, slot)
        return True, 'loaded'

    monkeypatch.setattr(provider, 'load', load, raising=False)
    monkeypatch.setattr('bluesky.settings.meteo_time_autoupdate', True)
    provider.get_atmosphere([10.5], [179.5], [500.0], start + timedelta(hours=1))
    assert loaded == [start + timedelta(hours=1)]
    assert provider.active_slot == (start + timedelta(hours=1)).isoformat()


def test_temporal_interpolation_is_explicit_and_thermodynamically_consistent(monkeypatch):
    provider = MeteorologyProvider()
    provider.slot_hours = 1
    provider.request_bounds = None
    start = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    first = cube()
    second = WeatherCube(
        first.altitude, first.latitude, first.longitude,
        first.east_wind + 2.0, first.north_wind + 4.0,
        first.temperature + 10.0, first.pressure - 1000.0,
        'SYNTHETIC', '2026-01-01T13:00:00+00:00')
    provider.set_cube(first, None, start, second)
    monkeypatch.setattr('bluesky.settings.meteo_time_interpolation', True)
    north, east, sample = provider._interpolate(
        [10.5], [180.0], [500.0], start + timedelta(minutes=30))
    first_north, first_east, first_sample = first.interpolate([10.5], [180.0], [500.0])
    assert north[0] == pytest.approx(first_north[0] + 2.0)
    assert east[0] == pytest.approx(first_east[0] + 1.0)
    assert sample.temperature[0] == pytest.approx(first_sample.temperature[0] + 5.0)
    assert sample.pressure[0] == pytest.approx(first_sample.pressure[0] - 500.0)
    assert sample.density[0] == pytest.approx(
        sample.pressure[0] / (R * sample.temperature[0]))
    assert sample.dataset_time.endswith('@0.500000')


def test_meteoconfig_controls_runtime_policy(monkeypatch):
    provider = MeteorologyProvider()
    monkeypatch.setattr(provider, 'strict', False)
    success, message = provider.configure('STRICT', 'ON')
    assert success and provider.strict and 'strict=True' in message
    success, message = provider.configure('TIMEUPDATE', 'OFF')
    assert success and not bs.settings.meteo_time_autoupdate
    success, message = provider.configure('INTERPOLATION', 'ON')
    assert success and bs.settings.meteo_time_interpolation
    success, message = provider.configure('UNKNOWN', 'ON')
    assert not success and 'STRICT' in message


def test_provider_default_caches_resolve_to_concrete_paths(monkeypatch, tmp_path):
    class MultipleRoots:
        def as_posix(self):
            return str(tmp_path)

    monkeypatch.setattr(bs, 'resource', lambda name: MultipleRoots())
    monkeypatch.setattr('bluesky.settings.era5_cache_path', '')
    monkeypatch.setattr('bluesky.settings.gfs_cache_path', '')
    era5 = object.__new__(WindECMWF)
    WindECMWF.__init__(era5)
    gfs = object.__new__(WindGFS)
    WindGFS.__init__(gfs)
    assert era5.cache == tmp_path / 'weather' / 'era5'
    assert gfs.cache == tmp_path / 'weather' / 'gfs'


def test_gfs_command_accepts_explicit_analysis_cycle(monkeypatch):
    provider = object.__new__(WindGFS)
    captured = {}

    def load(*bounds, slot=None):
        captured['bounds'] = bounds
        captured['slot'] = slot
        return True, 'loaded'

    monkeypatch.setattr(provider, 'load', load)
    success, _ = provider.load_command(40.0, -5.0, 45.0, 5.0, '20260804', '06')
    assert success
    assert captured['bounds'] == (40.0, -5.0, 45.0, 5.0)
    assert captured['slot'] == datetime(2026, 8, 4, 6)


@pytest.mark.parametrize(('date', 'cycle'), [
    ('20260804', ''), ('', '06'), ('20260804', '03'), ('20260230', '06')])
def test_gfs_command_rejects_incomplete_or_invalid_cycle(date, cycle):
    provider = object.__new__(WindGFS)
    success, message = provider.load_command(40.0, -5.0, 45.0, 5.0, date, cycle)
    assert not success
    assert 'GFS' in message


@pytest.mark.parametrize(('source', 'expected'), [
    ('NCEI', '202608/20260804/gfsanl_3_20260804_0600_000.grb2'),
    ('AWS', 'gfs.20260804/06/atmos/gfs.t06z.pgrb2.1p00.f000')])
def test_gfs_source_location(monkeypatch, tmp_path, source, expected):
    provider = object.__new__(WindGFS)
    provider.cache = tmp_path
    monkeypatch.setattr('bluesky.settings.windgfs_source', source)
    monkeypatch.setattr('bluesky.settings.windgfs_url', '')
    url, target = provider._location(datetime(2026, 8, 4, 6))
    assert url.endswith(expected)
    assert target.parent == tmp_path


def test_gfs_source_location_rejects_unknown_source(monkeypatch, tmp_path):
    provider = object.__new__(WindGFS)
    provider.cache = tmp_path
    monkeypatch.setattr('bluesky.settings.windgfs_source', 'UNKNOWN')
    with pytest.raises(ValueError, match='NCEI or AWS'):
        provider._location(datetime(2026, 8, 4, 6))


def test_gfs_fetch_reuses_valid_cached_file(monkeypatch, tmp_path):
    provider = object.__new__(WindGFS)
    provider.cache = tmp_path
    slot = datetime(2026, 8, 4, 6)
    target = tmp_path / 'weather.grb2'
    target.write_bytes(b'cached')
    monkeypatch.setattr(provider, '_location', lambda ignored: ('url', target))
    monkeypatch.setattr(provider, '_validate', lambda path: None)
    monkeypatch.setattr('bluesky.plugins.windgfs.atomic_download',
                        lambda *args: pytest.fail('cache hit opened the network'))
    assert provider._fetch(slot) == target


def test_gfs_interpolation_loads_exact_six_hour_successor(monkeypatch):
    provider = object.__new__(WindGFS)
    MeteorologyProvider.__init__(provider)
    provider.request_bounds = None
    start = datetime(2025, 8, 15, 12, tzinfo=timezone.utc)
    fetched = []

    def fetch(slot):
        fetched.append(slot)
        return slot

    def read(path, slot):
        result = cube()
        result.source = 'GFS'
        result.dataset_time = slot.isoformat()
        return result

    monkeypatch.setattr(provider, '_fetch', fetch)
    monkeypatch.setattr(provider, '_read', read)
    monkeypatch.setattr('bluesky.settings.meteo_time_interpolation', True)
    success, _ = provider.load(10.0, 179.0, 11.0, -179.0, slot=start)
    assert success
    assert fetched == [start, start + timedelta(hours=6)]
    assert provider.current_slot == start
    assert provider.next_cube.dataset_time == (start + timedelta(hours=6)).isoformat()


def test_era5_fetch_reuses_valid_cached_file(monkeypatch, tmp_path):
    provider = object.__new__(WindECMWF)
    provider.cache = tmp_path
    slot = datetime(2026, 8, 4, 6)
    bounds = (40.0, -5.0, 45.0, 5.0)
    target = provider._path(slot, bounds, 0)
    target.write_bytes(b'cached')
    sentinel = object()
    monkeypatch.setattr(provider, '_read', lambda path, ignored: sentinel)
    assert provider._fetch(slot, bounds) is sentinel
