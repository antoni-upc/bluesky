"""BlueSky provider lifecycle shared by ERA5 and GFS."""

import numpy as np

import bluesky as bs
from bluesky import stack
from bluesky.traffic.windsim import WindSim
from bluesky.traffic.atmosphere import AtmosphereSample
from bluesky.traffic.windfield import Windfield


bs.settings.set_variable_defaults(meteo_strict=False)


def previous_slot(utc, hours=3):
    """Latest analysis slot not later than UTC."""
    return utc.replace(hour=(utc.hour // hours) * hours, minute=0, second=0, microsecond=0)


class MeteorologyProvider(WindSim):
    source = 'METEO'

    def __init__(self):
        super().__init__()
        self.cube = None
        self.active_slot = None
        self.bounds = None
        self.strict = bool(bs.settings.meteo_strict)
        self.failure_counts = {}

    @property
    def winddim(self):
        return 3 if self.cube is not None else 0

    @winddim.setter
    def winddim(self, value):
        # Windfield initialization writes this value; cube presence is authoritative.
        pass

    def clear(self):
        Windfield.clear(self)
        self.cube = None
        self.active_slot = None

    def set_cube(self, cube, bounds=None):
        self.cube = cube
        self.active_slot = cube.dataset_time
        self.bounds = bounds

    def _inside_bounds(self, lat, lon):
        if self.bounds is None:
            return np.ones(np.broadcast(lat, lon).shape, dtype=bool)
        lat0, lon0, lat1, lon1 = self.bounds
        lat_lo, lat_hi = sorted((lat0, lat1))
        wrapped = (np.asarray(lon) + 360.0) % 360.0
        west, east = (lon0 + 360.0) % 360.0, (lon1 + 360.0) % 360.0
        lon_ok = ((wrapped >= west) & (wrapped <= east)) if west <= east else \
            ((wrapped >= west) | (wrapped <= east))
        return (np.asarray(lat) >= lat_lo) & (np.asarray(lat) <= lat_hi) & lon_ok

    def getdata(self, lat, lon, alt=0.0):
        if self.cube is None:
            shape = np.broadcast(lat, lon, alt).shape
            return np.zeros(shape), np.zeros(shape)
        north, east, _ = self.cube.interpolate(lat, lon, alt)
        inside = self._inside_bounds(lat, lon)
        # Out-of-domain wind is an explicit zero only because Traffic also
        # receives an invalid atmosphere flag for the same point.
        return np.where(inside, np.nan_to_num(north), 0.0), \
            np.where(inside, np.nan_to_num(east), 0.0)

    def get_atmosphere(self, lat, lon, alt, utc):
        if self.cube is None:
            return None
        _, _, sample = self.cube.interpolate(lat, lon, alt)
        valid = sample.valid & self._inside_bounds(lat, lon)
        sample = AtmosphereSample(sample.temperature, sample.pressure, sample.density,
                                  valid, sample.source, sample.dataset_time,
                                  'OUTSIDE_REQUESTED_DOMAIN')
        invalid_count = int(np.count_nonzero(~valid))
        if invalid_count:
            reason = sample.fallback_reason
            previous = self.failure_counts.get(reason, 0)
            self.failure_counts[reason] = previous + invalid_count
            if previous == 0:
                stack.echo(f'{self.source}: {invalid_count} sample(s) using ISA fallback: {reason}')
        if self.strict and not np.all(sample.valid):
            raise RuntimeError(f'{self.source} requested outside its validated domain')
        return sample

    def desired_slot(self, utc):
        return previous_slot(utc)
