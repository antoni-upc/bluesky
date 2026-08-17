"""GFS atmosphere/wind provider backed by shared validated interpolation."""

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

import bluesky as bs
from bluesky import stack
from bluesky.plugins.meteo import MeteorologyProvider, WeatherCube
from bluesky.plugins.meteo.download import atomic_download


bs.settings.set_variable_defaults(
    gfs_cache_path='',
    windgfs_source='NCEI',
    windgfs_url='')

GFS_BASE_URLS = {
    'NCEI': 'https://www.ncei.noaa.gov/data/global-forecast-system/access/historical/analysis/',
    'AWS': 'https://noaa-gfs-bdp-pds.s3.amazonaws.com/',
}


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
    slot_hours = 6

    def __init__(self):
        super().__init__()
        configured = bs.settings.gfs_cache_path
        cache_root = Path(bs.resource('cache').as_posix())
        self.cache = (Path(configured).expanduser() if configured else
                      cache_root / 'weather' / 'gfs')
        self.cache.mkdir(parents=True, exist_ok=True)
        probe = self.cache / '.write-capability'
        probe.write_text('ok', encoding='ascii')
        probe.unlink()
        self.request_bounds = None

    def _location(self, slot):
        source = str(bs.settings.windgfs_source).upper()
        if source == 'NCEI':
            name = f'gfsanl_3_{slot:%Y%m%d}_{slot:%H}00_000.grb2'
            remote = f'{slot:%Y%m}/{slot:%Y%m%d}/{name}'
        elif source == 'AWS':
            name = f'gfs.t{slot:%H}z.pgrb2.1p00.f000'
            remote = f'gfs.{slot:%Y%m%d}/{slot:%H}/atmos/{name}'
            # Include the date in the cache name because AWS reuses the product filename daily.
            name = f'gfs.{slot:%Y%m%d}.t{slot:%H}z.pgrb2.1p00.f000'
        else:
            raise ValueError(f'Unknown GFS source {source!r}; expected NCEI or AWS')
        base = bs.settings.windgfs_url or GFS_BASE_URLS[source]
        return base.rstrip('/') + '/' + remote, self.cache / name

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
                stack.echo(f'GFS: cached file is invalid; removing {target}')
                target.unlink()
        stack.echo(
            f'GFS: not in cache; downloading slot={slot.isoformat()} '
            f'source={bs.settings.windgfs_source} url={url} to {target}')
        result = atomic_download(requests.Session(), url, target, self._validate)
        stack.echo(f'GFS: download validated and cached at {target}')
        return result

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
        next_cube = None
        if bs.settings.meteo_time_interpolation:
            next_slot = slot + timedelta(hours=self.slot_hours)
            next_cube = self._read(self._fetch(next_slot), next_slot)
        self.set_cube(cube, self.request_bounds, slot, next_cube)
        mode = ' with temporal interpolation' if next_cube is not None else ''
        return True, f'GFS {slot.isoformat()} loaded and validated{mode}'

    @stack.command(name='WINDGFS')
    def load_command(self, lat0: 'lat', lon0: 'lon', lat1: 'lat', lon1: 'lon',
                     date: str = '', cycle: str = ''):
        """Load GFS for simulation UTC or an explicit YYYYMMDD and 00/06/12/18 cycle."""
        if bool(date) != bool(cycle):
            return False, 'Provide both GFS date YYYYMMDD and cycle 00, 06, 12, or 18'
        if not date:
            return self.load(lat0, lon0, lat1, lon1)
        try:
            slot = datetime.strptime(f'{date}{cycle}', '%Y%m%d%H')
        except ValueError:
            return False, 'Invalid GFS date/cycle; use YYYYMMDD and 00, 06, 12, or 18'
        if slot.hour not in (0, 6, 12, 18):
            return False, 'Invalid GFS cycle; expected 00, 06, 12, or 18'
        return self.load(lat0, lon0, lat1, lon1, slot=slot)
