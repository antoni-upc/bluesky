"""GFS atmosphere/wind provider backed by shared validated interpolation."""

from pathlib import Path

import numpy as np

import bluesky as bs
from bluesky import stack
from bluesky.core import timed_function
from bluesky.plugins.meteo import MeteorologyProvider, WeatherCube
from bluesky.plugins.meteo.download import atomic_download


bs.settings.set_variable_defaults(
    gfs_cache_path='',
    windgfs_url='https://www.ncei.noaa.gov/data/global-forecast-system/access/historical/analysis/')


def init_plugin():
    try:
        import pygrib  # noqa: F401
        import requests  # noqa: F401
        provider = WindGFS()
    except Exception as exc:
        raise ImportError(f'WINDGFS unavailable; check GFS dependencies and cache: {exc}') from exc
    WindGFS.select(provider)
    return {'plugin_name': 'WINDGFS', 'plugin_type': 'sim'}


class WindGFS(MeteorologyProvider):
    source = 'GFS'

    def __init__(self):
        super().__init__()
        configured = bs.settings.gfs_cache_path
        self.cache = Path(configured).expanduser() if configured else bs.resource('grib')
        self.cache.mkdir(parents=True, exist_ok=True)
        probe = self.cache / '.write-capability'
        probe.write_text('ok', encoding='ascii')
        probe.unlink()
        self.request_bounds = None

    def _location(self, slot):
        name = f'gfsanl_3_{slot:%Y%m%d}_{slot:%H}00_000.grb2'
        remote = f'{slot:%Y%m}/{slot:%Y%m%d}/{name}'
        return bs.settings.windgfs_url.rstrip('/') + '/' + remote, self.cache / name

    @staticmethod
    def _validate(path):
        import pygrib
        with pygrib.open(str(path)) as grib:
            for name in ('u', 'v', 't', 'gh'):
                if not grib.select(shortName=name, typeOfLevel='isobaricInhPa'):
                    raise ValueError(f'Missing GFS {name} pressure-level messages')

    def _fetch(self, slot):
        import requests
        url, target = self._location(slot)
        if target.exists():
            try:
                self._validate(target)
                return target
            except (OSError, ValueError, RuntimeError):
                target.unlink()
        return atomic_download(requests.Session(), url, target, self._validate)

    def _read(self, path, slot):
        import pygrib
        with pygrib.open(str(path)) as grib:
            groups = {name: grib.select(shortName=name, typeOfLevel='isobaricInhPa')
                      for name in ('u', 'v', 't', 'gh')}
            level_sets = [{message.level for message in messages} for messages in groups.values()]
            levels = sorted(set.intersection(*level_sets), reverse=True)
            if len(levels) < 2:
                raise ValueError('GFS variables do not share at least two pressure levels')
            lookup = {name: {m.level: m for m in messages} for name, messages in groups.items()}
            first = lookup['u'][levels[0]]
            lat2d, lon2d = first.latlons()
            lat, lon = lat2d[:, 0], lon2d[0, :]
            fields = [np.stack([lookup[name][level].values for level in levels])
                      for name in ('u', 'v', 't', 'gh')]
        return WeatherCube.from_pressure_levels(np.asarray(levels) * 100.0, lat, lon,
            fields[3], fields[0], fields[1], fields[2], self.source, slot.isoformat())

    def load(self, lat0, lon0, lat1, lon1, slot=None):
        slot = self.desired_slot(slot or bs.sim.utc)
        cube = self._read(self._fetch(slot), slot)
        self.request_bounds = (lat0, lon0, lat1, lon1)
        self.set_cube(cube, self.request_bounds)
        return True, f'GFS {slot.isoformat()} loaded and validated'

    @stack.command(name='WINDGFS')
    def load_command(self, lat0: 'lat', lon0: 'lon', lat1: 'lat', lon1: 'lon'):
        return self.load(lat0, lon0, lat1, lon1)

    @timed_function(name='WINDGFS_update', dt=60)
    def update(self):
        slot = self.desired_slot(bs.sim.utc)
        if self.request_bounds and slot.isoformat() != self.active_slot:
            self.load(*self.request_bounds, slot=slot)
