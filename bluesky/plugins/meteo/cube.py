"""Validated, non-extrapolating meteorological interpolation."""

from dataclasses import dataclass

import numpy as np
from scipy.interpolate import RegularGridInterpolator

from bluesky.tools.aero import R
from bluesky.traffic.atmosphere import AtmosphereSample


class GridValidationError(ValueError):
    pass


def _axis(name, values):
    values = np.asarray(values, dtype=float)
    if values.ndim != 1 or values.size < 2 or not np.all(np.isfinite(values)):
        raise GridValidationError(f'{name} must be a finite one-dimensional axis')
    order = np.argsort(values)
    sorted_values = values[order]
    if np.any(np.diff(sorted_values) <= 0.0):
        raise GridValidationError(f'{name} must be strictly monotonic and unique')
    return sorted_values, order


def _longitude_axis(values):
    """Order a circular longitude axis across its largest unrepresented gap."""
    wrapped = (np.asarray(values, dtype=float) + 360.0) % 360.0
    sorted_values, order = _axis('longitude', wrapped)
    gaps = np.concatenate((np.diff(sorted_values),
                           [sorted_values[0] + 360.0 - sorted_values[-1]]))
    start = (int(np.argmax(gaps)) + 1) % len(sorted_values)
    if start == 0:
        return sorted_values, order
    axis = np.concatenate((sorted_values[start:], sorted_values[:start] + 360.0))
    permutation = np.concatenate((order[start:], order[:start]))
    return axis, permutation


@dataclass
class WeatherCube:
    altitude: np.ndarray
    latitude: np.ndarray
    longitude: np.ndarray
    east_wind: np.ndarray
    north_wind: np.ndarray
    temperature: np.ndarray
    pressure: np.ndarray
    source: str
    dataset_time: str

    @classmethod
    def from_pressure_levels(cls, levels_pa, latitude, longitude, geopotential_height,
                             east_wind, north_wind, temperature, source, dataset_time):
        """Resample spatially varying pressure levels to a common height axis."""
        levels_pa = np.asarray(levels_pa, dtype=float)
        height = np.asarray(geopotential_height, dtype=float)
        expected = (levels_pa.size, len(latitude), len(longitude))
        if height.shape != expected:
            raise GridValidationError(f'geopotential height shape {height.shape} does not match {expected}')
        lower = float(np.max(np.nanmin(height, axis=0)))
        upper = float(np.min(np.nanmax(height, axis=0)))
        if not lower < upper:
            raise GridValidationError('pressure-level columns have no common vertical domain')
        target = np.linspace(lower, upper, levels_pa.size)
        source_fields = [np.asarray(np.ma.asarray(value).filled(np.nan), dtype=float)
                         for value in (east_wind, north_wind, temperature)]
        source_fields.append(np.broadcast_to(levels_pa[:, None, None], expected))
        if any(value.shape != expected for value in source_fields):
            raise GridValidationError(f'pressure-level fields must all have shape {expected}')
        output = [np.empty(expected, dtype=float) for _ in source_fields]
        for iy in range(expected[1]):
            for ix in range(expected[2]):
                order = np.argsort(height[:, iy, ix])
                hcol = height[order, iy, ix]
                if np.any(np.diff(hcol) <= 0.0):
                    raise GridValidationError(f'non-monotonic height column at ({iy}, {ix})')
                for dst, src in zip(output, source_fields):
                    column = src[:, iy, ix][order]
                    dst[:, iy, ix] = np.interp(target, hcol, column, left=np.nan, right=np.nan)
        return cls(target, latitude, longitude, *output, source, dataset_time)

    def __post_init__(self):
        self.altitude, iz = _axis('altitude', self.altitude)
        self.latitude, iy = _axis('latitude', self.latitude)
        self.longitude, ix = _longitude_axis(self.longitude)
        shape = (len(iz), len(iy), len(ix))
        fields = []
        for name in ('east_wind', 'north_wind', 'temperature', 'pressure'):
            value = np.ma.asarray(getattr(self, name))
            if value.shape != shape:
                raise GridValidationError(f'{name} shape {value.shape} does not match {shape}')
            value = np.asarray(value.filled(np.nan), dtype=float)[np.ix_(iz, iy, ix)]
            if not np.all(np.isfinite(value)):
                raise GridValidationError(f'{name} contains missing or non-finite cells')
            fields.append(value)
            setattr(self, name, value)
        if np.any(self.temperature <= 0.0) or np.any(self.pressure <= 0.0):
            raise GridValidationError('temperature and pressure must be positive')
        axes = (self.altitude, self.latitude, self.longitude)
        self._interpolators = [RegularGridInterpolator(axes, value,
            bounds_error=False, fill_value=np.nan) for value in fields]

    def _points(self, lat, lon, alt):
        lat, lon, alt = np.broadcast_arrays(lat, lon, alt)
        wrapped = (np.asarray(lon, dtype=float) + 360.0) % 360.0
        wrapped = np.where(wrapped < self.longitude[0], wrapped + 360.0, wrapped)
        return np.column_stack((np.ravel(alt), np.ravel(lat), np.ravel(wrapped))), lat.shape

    def interpolate(self, lat, lon, alt):
        points, shape = self._points(lat, lon, alt)
        east, north, temperature, pressure = [fn(points).reshape(shape) for fn in self._interpolators]
        valid = np.isfinite(east) & np.isfinite(north) & np.isfinite(temperature) & np.isfinite(pressure)
        density = np.where(valid, pressure / (R * temperature), np.nan)
        sample = AtmosphereSample(temperature, pressure, density, valid,
                                  self.source, self.dataset_time, 'OUTSIDE_DOMAIN')
        return north, east, sample
