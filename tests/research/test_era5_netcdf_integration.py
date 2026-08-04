from datetime import datetime, timezone

import numpy as np
import pytest

netcdf = pytest.importorskip('netCDF4')

from bluesky.plugins.windecmwf import WindECMWF


def test_real_netcdf_reader_builds_valid_cube(tmp_path):
    path = tmp_path / 'synthetic-era5.nc'
    with netcdf.Dataset(path, 'w') as ds:
        ds.createDimension('time', 1)
        ds.createDimension('pressure_level', 2)
        ds.createDimension('latitude', 2)
        ds.createDimension('longitude', 2)
        time = ds.createVariable('time', 'f8', ('time',))
        time.units = 'hours since 2026-01-01 00:00:00'
        time[:] = [0]
        level = ds.createVariable('pressure_level', 'f8', ('pressure_level',))
        level.units = 'hPa'
        level[:] = [1000.0, 900.0]
        ds.createVariable('latitude', 'f8', ('latitude',))[:] = [40.0, 41.0]
        ds.createVariable('longitude', 'f8', ('longitude',))[:] = [1.0, 2.0]
        shape = ('time', 'pressure_level', 'latitude', 'longitude')
        units = {'u': 'm/s', 'v': 'm/s', 't': 'K', 'z': 'm**2 s**-2'}
        values = {
            'u': np.array([[[1.0, 2.0], [3.0, 4.0]], [[5.0, 6.0], [7.0, 8.0]]]),
            'v': np.array([[[2.0, 3.0], [4.0, 5.0]], [[6.0, 7.0], [8.0, 9.0]]]),
            't': np.array([[[280.0, 281.0], [282.0, 283.0]], [[275.0, 276.0], [277.0, 278.0]]]),
            'z': np.array([[[100.0, 110.0], [120.0, 130.0]],
                           [[1000.0, 1010.0], [1020.0, 1030.0]]]) * 9.80665}
        for name, value in values.items():
            variable = ds.createVariable(name, 'f8', shape)
            variable.units = units[name]
            variable[:] = value[None, ...]
    provider = object.__new__(WindECMWF)
    cube = provider._read(path, datetime(2026, 1, 1, tzinfo=timezone.utc))
    north, east, sample = cube.interpolate([40.5], [1.5], [565.0])
    assert sample.valid[0]
    assert np.isfinite(north[0]) and np.isfinite(east[0])
    assert sample.source == 'ERA5'
