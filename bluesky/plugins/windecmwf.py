"""ERA5 atmosphere/wind provider backed by shared validated interpolation."""

from pathlib import Path
import os

import numpy as np

import bluesky as bs
from bluesky import stack
from bluesky.core import timed_function
from bluesky.plugins.meteo import MeteorologyProvider, WeatherCube


bs.settings.set_variable_defaults(era5_cache_path='', era5_pressure_levels=[
    100, 125, 150, 175, 200, 225, 250, 300, 350, 400, 450, 500, 550, 600,
    650, 700, 750, 775, 800])


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

    def __init__(self):
        super().__init__()
        configured = bs.settings.era5_cache_path
        self.cache = Path(configured).expanduser() if configured else bs.resource('NetCDF')
        self.cache.mkdir(parents=True, exist_ok=True)
        probe = self.cache / '.write-capability'
        probe.write_text('ok', encoding='ascii')
        probe.unlink()
        self.request_bounds = None

    def _path(self, slot):
        return self.cache / f'p_levels_{slot:%Y%m%d}.nc'

    def _fetch(self, slot):
        import cdsapi
        target = self._path(slot)
        if target.exists():
            try:
                return target, self._read(target, slot)
            except (OSError, ValueError, KeyError, IndexError):
                target.unlink()
        part = target.with_suffix('.nc.part')
        part.unlink(missing_ok=True)
        try:
            cdsapi.Client().retrieve('reanalysis-era5-pressure-levels', {
                'product_type': 'reanalysis', 'format': 'netcdf',
                'pressure_level': [str(x) for x in bs.settings.era5_pressure_levels],
                'year': f'{slot.year:04d}', 'month': f'{slot.month:02d}', 'day': f'{slot.day:02d}',
                'time': [f'{h:02d}:00' for h in range(0, 24, 3)],
                'variable': ['u_component_of_wind', 'v_component_of_wind',
                             'temperature', 'geopotential']}, str(part))
            cube = self._read(part, slot)  # validate before accepting cache
            part.replace(target)
        except Exception:
            part.unlink(missing_ok=True)
            raise
        return target, cube

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

    def load(self, lat0, lon0, lat1, lon1, slot=None):
        slot = self.desired_slot(slot or bs.sim.utc)
        path, cube = self._fetch(slot)
        cube = cube or self._read(path, slot)
        self.request_bounds = (lat0, lon0, lat1, lon1)
        self.set_cube(cube, self.request_bounds)
        return True, f'ERA5 {slot.isoformat()} loaded and validated'

    @stack.command(name='WINDECMWF')
    def load_command(self, lat0: 'lat', lon0: 'lon', lat1: 'lat', lon1: 'lon'):
        return self.load(lat0, lon0, lat1, lon1)

    @timed_function(name='WINDECMWF_update', dt=60)
    def update(self):
        slot = self.desired_slot(bs.sim.utc)
        if self.request_bounds and slot.isoformat() != self.active_slot:
            self.load(*self.request_bounds, slot=slot)
