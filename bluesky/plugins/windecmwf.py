"""ERA5 atmosphere/wind provider backed by shared validated interpolation."""

from pathlib import Path
import hashlib
import json
import os
import re
from datetime import timedelta

import numpy as np

import bluesky as bs
from bluesky import stack
from bluesky.plugins.meteo import MeteorologyProvider, WeatherCube


bs.settings.set_variable_defaults(era5_cache_path='', era5_region='region', era5_pressure_levels=[
    100, 125, 150, 175, 200, 225, 250, 300, 350, 400, 450, 500, 550, 600,
    650, 700, 750, 775, 800, 825, 850, 875, 900, 925, 950, 975, 1000])


def init_plugin():
    try:
        import cdsapi
        import netCDF4  # noqa: F401
        credential_file = Path(os.environ.get('CDSAPI_RC', '~/.cdsapirc')).expanduser()
        if not credential_file.is_file() and not os.environ.get('CDSAPI_KEY'):
            raise RuntimeError(f'CDS credentials not found at {credential_file}')
        provider = WindECMWF()
    except Exception as exc:
        raise ImportError(f'WINDECMWF unavailable; check ERA5 dependencies, credentials, and cache: {exc}') from exc
    WindECMWF.select(provider)
    return {'plugin_name': 'WINDECMWF', 'plugin_type': 'sim'}


class WindECMWF(MeteorologyProvider):
    source = 'ERA5'
    slot_hours = 1

    def __init__(self):
        super().__init__()
        configured = bs.settings.era5_cache_path
        cache_root = Path(bs.resource('cache').as_posix())
        self.cache = (Path(configured).expanduser() if configured else
                      cache_root / 'weather' / 'era5')
        self.cache.mkdir(parents=True, exist_ok=True)
        probe = self.cache / '.write-capability'
        probe.write_text('ok', encoding='ascii')
        probe.unlink()
        self.request_bounds = None

    @staticmethod
    def _areas(bounds):
        lat0, lon0, lat1, lon1 = bounds
        north, south = max(lat0, lat1), min(lat0, lat1)
        west, east = (lon0 + 180.0) % 360.0 - 180.0, (lon1 + 180.0) % 360.0 - 180.0
        if west <= east:
            return [(north, west, south, east)]
        return [(north, west, south, 180.0), (north, -180.0, south, east)]

    @staticmethod
    def _region_label():
        label = str(bs.settings.era5_region).lower().strip()
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', label):
            raise ValueError('era5_region must contain lowercase letters, numbers, and hyphens')
        return label

    @classmethod
    def _request_identity(cls, slot, area):
        request = cls._request(slot, area)
        return json.dumps(request, sort_keys=True, separators=(',', ':')).encode('utf-8')

    def _path(self, slot, bounds, part):
        areas = self._areas(bounds)
        try:
            area = areas[part]
        except IndexError as exc:
            raise ValueError(f'ERA5 request part {part} does not exist') from exc
        digest = hashlib.sha256(self._request_identity(slot, area)).hexdigest()[:12]
        levels = [int(value) for value in bs.settings.era5_pressure_levels]
        pressure_range = f'p{min(levels)}-{max(levels)}'
        return self.cache / (
            f'era5-pressure-levels_{self._region_label()}_{slot:%Y%m%dT%H%MZ}_'
            f'{pressure_range}_{digest}_part{part}.nc')

    @staticmethod
    def _request(slot, area):
        return {
            'product_type': ['reanalysis'], 'data_format': 'netcdf',
            'download_format': 'unarchived',
            'pressure_level': [str(x) for x in bs.settings.era5_pressure_levels],
            'year': [f'{slot.year:04d}'], 'month': [f'{slot.month:02d}'],
            'day': [f'{slot.day:02d}'], 'time': [f'{slot.hour:02d}:00'],
            'area': [float(value) for value in area],
            'variable': ['u_component_of_wind', 'v_component_of_wind',
                         'temperature', 'geopotential']}

    def _fetch(self, slot, bounds):
        import cdsapi
        cubes = []
        for index, area in enumerate(self._areas(bounds)):
            target = self._path(slot, bounds, index)
            if target.exists():
                try:
                    cubes.append(self._read_validated(target, slot, area))
                    continue
                except (OSError, ValueError, KeyError, IndexError):
                    stack.echo(f'ERA5: cached file is invalid; removing {target}')
                    target.unlink()
            part = target.with_suffix('.nc.part')
            part.unlink(missing_ok=True)
            try:
                levels = ','.join(str(level) for level in bs.settings.era5_pressure_levels)
                stack.echo(
                    f'ERA5: not in cache; downloading slot={slot.isoformat()} '
                    f'area={list(area)} pressure_levels_hPa=[{levels}] to {target}')
                cdsapi.Client().retrieve('reanalysis-era5-pressure-levels',
                                         self._request(slot, area), str(part))
                cube = self._read_validated(part, slot, area)
                part.replace(target)
                stack.echo(f'ERA5: download validated and cached at {target}')
                cubes.append(cube)
            except Exception:
                part.unlink(missing_ok=True)
                raise
        return cubes[0] if len(cubes) == 1 else self._merge(cubes, slot)

    def _read_validated(self, path, slot, area):
        """Validate request identity stored in a cache file before accepting it."""
        import netCDF4 as nc
        with nc.Dataset(path, mode='r') as ds:
            level_var = self._variable(ds, 'pressure_level', 'level')
            self._units(level_var, {'hpa', 'millibars', 'mbar'}, 'pressure level')
            stored_levels = sorted(float(value) for value in np.asarray(level_var[:]).ravel())
            expected_levels = sorted(float(value) for value in bs.settings.era5_pressure_levels)
            if stored_levels != expected_levels:
                raise ValueError(
                    f'ERA5 pressure levels differ: stored={stored_levels}, '
                    f'expected={expected_levels}')
            latitude = np.asarray(self._variable(ds, 'latitude')[:], dtype=float)
            longitude = np.asarray(self._variable(ds, 'longitude')[:], dtype=float)
            north, west, south, east = area
            relative_longitude = (longitude - west) % 360.0
            requested_span = (east - west) % 360.0
            if (latitude.min() > south or latitude.max() < north or
                    relative_longitude.min() > 1e-9 or
                    relative_longitude.max() + 1e-9 < requested_span):
                raise ValueError(f'ERA5 grid does not cover requested area {list(area)}')
            times = self._variable(ds, 'valid_time', 'time')
            calendar = getattr(times, 'calendar', 'standard')
            target_time = nc.date2num(slot.replace(tzinfo=None), times.units, calendar)
            if not np.any(np.isclose(np.asarray(times[:], dtype=float), target_time)):
                raise ValueError(f'ERA5 file does not contain exact slot {slot.isoformat()}')
        return self._read(path, slot)

    @staticmethod
    def _variable(dataset, *names):
        for name in names:
            if name in dataset.variables:
                return dataset.variables[name]
        raise ValueError(f'Missing required ERA5 variable: {names}')

    @staticmethod
    def _units(variable, allowed, name):
        units = str(getattr(variable, 'units', '')).lower().replace(' ', '')
        if units not in allowed:
            raise ValueError(f'ERA5 {name} has unsupported units {units!r}')

    def _read(self, path, slot):
        import netCDF4 as nc
        with nc.Dataset(path, mode='r') as ds:
            level_var = self._variable(ds, 'pressure_level', 'level')
            self._units(level_var, {'hpa', 'millibars', 'mbar'}, 'pressure level')
            levels = np.asarray(level_var[:], dtype=float) * 100.0
            lat = np.asarray(self._variable(ds, 'latitude')[:], dtype=float)
            lon = np.asarray(self._variable(ds, 'longitude')[:], dtype=float)
            times = self._variable(ds, 'valid_time', 'time')
            calendar = getattr(times, 'calendar', 'standard')
            target_time = nc.date2num(slot.replace(tzinfo=None), times.units, calendar)
            tidx = int(np.argmin(np.abs(np.asarray(times[:], dtype=float) - target_time)))
            variables = [self._variable(ds, name) for name in ('u', 'v', 't', 'z')]
            self._units(variables[0], {'m/s', 'ms**-1', 'ms-1'}, 'east wind')
            self._units(variables[1], {'m/s', 'ms**-1', 'ms-1'}, 'north wind')
            self._units(variables[2], {'k', 'kelvin'}, 'temperature')
            self._units(variables[3], {'m**2s**-2', 'm2s-2', 'm^2s^-2'}, 'geopotential')
            fields = [np.ma.asarray(variable[tidx]) for variable in variables]
            if any(field.ndim != 3 for field in fields):
                raise ValueError('ERA5 fields must have (level, latitude, longitude) dimensions')
            east, north, temp, geopotential = fields
            return WeatherCube.from_pressure_levels(levels, lat, lon, geopotential / 9.80665,
                east, north, temp, self.source, slot.isoformat())

    @staticmethod
    def _merge(cubes, slot):
        """Merge antimeridian request parts already resampled to common heights."""
        first = cubes[0]
        if any(not np.array_equal(cube.latitude, first.latitude) for cube in cubes[1:]):
            raise ValueError('ERA5 antimeridian parts do not share a latitude axis')
        lower = max(float(cube.altitude[0]) for cube in cubes)
        upper = min(float(cube.altitude[-1]) for cube in cubes)
        if not lower < upper:
            raise ValueError('ERA5 antimeridian parts have no common vertical domain')
        altitude = np.linspace(lower, upper, min(len(cube.altitude) for cube in cubes))
        longitude = np.concatenate([cube.longitude for cube in cubes])
        wrapped = (longitude + 360.0) % 360.0
        _, keep = np.unique(wrapped, return_index=True)
        keep.sort()
        fields = []
        for name in ('east_wind', 'north_wind', 'temperature', 'pressure'):
            parts = []
            for cube in cubes:
                source = getattr(cube, name)
                output = np.empty((len(altitude), len(cube.latitude), len(cube.longitude)))
                for iy in range(len(cube.latitude)):
                    for ix in range(len(cube.longitude)):
                        output[:, iy, ix] = np.interp(
                            altitude, cube.altitude, source[:, iy, ix])
                parts.append(output)
            fields.append(np.concatenate(parts, axis=2)[:, :, keep])
        return WeatherCube(altitude, first.latitude, longitude[keep], *fields,
                           first.source, slot.isoformat())

    def load(self, lat0, lon0, lat1, lon1, slot=None):
        slot = self.desired_slot(slot or bs.sim.utc)
        self.request_bounds = (lat0, lon0, lat1, lon1)
        cube = self._fetch(slot, self.request_bounds)
        next_cube = None
        if bs.settings.meteo_time_interpolation:
            next_slot = slot + timedelta(hours=self.slot_hours)
            next_cube = self._fetch(next_slot, self.request_bounds)
        self.set_cube(cube, self.request_bounds, slot, next_cube)
        mode = ' with temporal interpolation' if next_cube is not None else ''
        return True, f'ERA5 {slot.isoformat()} loaded and validated{mode}'

    @stack.command(name='WINDECMWF')
    def load_command(self, lat0: 'lat', lon0: 'lon', lat1: 'lat', lon1: 'lon'):
        return self.load(lat0, lon0, lat1, lon1)
