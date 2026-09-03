"""BlueSky provider lifecycle shared by ERA5 and GFS."""

import numpy as np

import bluesky as bs
from bluesky import stack
from bluesky.traffic.windsim import WindSim
from bluesky.traffic.atmosphere import AtmosphereSample
from bluesky.traffic.windfield import Windfield
from bluesky.tools.aero import R, vatmos


bs.settings.set_variable_defaults(
    meteo_strict=False,
    meteo_below_domain_policy='REJECT',
    meteo_time_autoupdate=True,
    meteo_time_interpolation=False)


BELOW_DOMAIN_POLICIES = {'REJECT', 'ISA', 'ISA_ANCHORED'}


def previous_slot(utc, hours=3):
    """Latest analysis slot not later than UTC."""
    return utc.replace(hour=(utc.hour // hours) * hours, minute=0, second=0, microsecond=0)


class MeteorologyProvider(WindSim):
    source = 'METEO'
    slot_hours = 3

    def __init__(self):
        super().__init__()
        self.cube = None
        self.next_cube = None
        self.current_slot = None
        self.active_slot = None
        self.bounds = None
        self.strict = bool(bs.settings.meteo_strict)
        self.below_domain_policy = self._validate_below_domain_policy(
            bs.settings.meteo_below_domain_policy)
        self.failure_counts = {}
        self.unavailable_reason = ''
        self.expired_slot = ''

    @staticmethod
    def _validate_below_domain_policy(value):
        policy = str(value).upper().strip()
        if policy not in BELOW_DOMAIN_POLICIES:
            allowed = ', '.join(sorted(BELOW_DOMAIN_POLICIES))
            raise ValueError(f'below-domain policy must be one of: {allowed}')
        if policy == 'ISA_ANCHORED':
            raise NotImplementedError(
                'ISA_ANCHORED is reserved but not implemented; use REJECT or ISA')
        return policy

    @staticmethod
    def _metadata(values):
        """Collapse uniform vector metadata while preserving mixed provenance."""
        values = np.asarray(values, dtype=object)
        if values.size and np.all(values == values.flat[0]):
            return str(values.flat[0])
        return values

    @property
    def winddim(self):
        return 3 if self.cube is not None else 0

    @winddim.setter
    def winddim(self, value):
        # Windfield initialization writes this value; cube presence is authoritative.
        pass

    def clear(self, reason=''):
        Windfield.clear(self)
        self.cube = None
        self.next_cube = None
        self.current_slot = None
        self.active_slot = None
        self.unavailable_reason = reason
        if getattr(bs, 'traf', None) is not None:
            bs.traf.update_atmosphere()

    def set_cube(self, cube, bounds=None, current_slot=None, next_cube=None):
        self.cube = cube
        self.next_cube = next_cube
        self.current_slot = current_slot
        self.unavailable_reason = ''
        self.expired_slot = ''
        self.active_slot = current_slot.isoformat() if current_slot is not None else cube.dataset_time
        self.bounds = bounds
        if getattr(bs, 'traf', None) is not None:
            bs.traf.update_atmosphere()

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

    def _ensure_time_slot(self, utc):
        """Select the dataset whose validity interval contains simulation UTC.

        A file stamped T is assumed to represent the complete half-open interval
        [T, T + slot_hours). For ERA5 this is T:00:00 through T:59:59.999...;
        for GFS it is the corresponding six-hour analysis interval.
        """
        if utc is None or getattr(self, 'request_bounds', None) is None:
            return
        slot = self.desired_slot(utc)
        if slot.isoformat() == self.active_slot:
            return
        if bs.settings.meteo_time_autoupdate:
            self.advance_time_slot(
                slot, lambda: self.load(*self.request_bounds, slot=slot))
        else:
            self.expire_time_slot(slot)

    def _interpolate(self, lat, lon, alt, utc):
        self._ensure_time_slot(utc)
        if self.cube is None:
            return None
        north, east, sample = self.cube.interpolate(lat, lon, alt)
        if not bs.settings.meteo_time_interpolation or self.next_cube is None:
            return north, east, sample
        next_north, next_east, next_sample = self.next_cube.interpolate(lat, lon, alt)
        elapsed = max(0.0, (utc - self.current_slot).total_seconds())
        weight = min(1.0, elapsed / (self.slot_hours * 3600.0))
        blend = lambda left, right: (1.0 - weight) * left + weight * right
        valid = sample.valid & next_sample.valid
        temperature = blend(sample.temperature, next_sample.temperature)
        pressure = blend(sample.pressure, next_sample.pressure)
        interpolated = AtmosphereSample(
            temperature, pressure, pressure / (R * temperature),
            valid, self.source,
            f'{sample.dataset_time}->{next_sample.dataset_time}@{weight:.6f}',
            '' if np.all(valid) else 'OUTSIDE_DOMAIN')
        return blend(north, next_north), blend(east, next_east), interpolated

    def getdata(self, lat, lon, alt=0.0):
        result = self._interpolate(lat, lon, alt, getattr(bs.sim, 'utc', None))
        if result is None:
            shape = np.broadcast(lat, lon, alt).shape
            return np.zeros(shape), np.zeros(shape)
        north, east, _ = result
        inside = self._inside_bounds(lat, lon)
        # Out-of-domain wind is an explicit zero only because Traffic also
        # receives an invalid atmosphere flag for the same point.
        return np.where(inside, np.nan_to_num(north), 0.0), \
            np.where(inside, np.nan_to_num(east), 0.0)

    def get_atmosphere(self, lat, lon, alt, utc):
        result = self._interpolate(lat, lon, alt, utc)
        if result is None:
            if self.unavailable_reason:
                shape = np.broadcast(lat, lon, alt).shape
                invalid = np.full(shape, np.nan)
                return AtmosphereSample(invalid, invalid.copy(), invalid.copy(),
                                        np.zeros(shape, dtype=bool), self.source, '',
                                        self.unavailable_reason)
            return None
        _, _, sample = result
        alt = np.broadcast_to(np.asarray(alt, dtype=float), np.asarray(sample.valid).shape)
        inside = self._inside_bounds(lat, lon)
        cube_lower = float(self.cube.altitude[0])
        cube_upper = float(self.cube.altitude[-1])
        below = inside & (alt < cube_lower)
        above = inside & (alt > cube_upper)

        temperature = np.asarray(sample.temperature, dtype=float).copy()
        pressure = np.asarray(sample.pressure, dtype=float).copy()
        density = np.asarray(sample.density, dtype=float).copy()
        valid = np.asarray(sample.valid, dtype=bool) & inside
        source = np.full(valid.shape, sample.source, dtype=object)
        dataset_time = np.full(valid.shape, sample.dataset_time, dtype=object)
        reason = np.full(valid.shape, '', dtype=object)
        reason[~inside] = 'OUTSIDE_REQUESTED_DOMAIN'
        reason[below] = 'BELOW_SOURCE_DOMAIN'
        reason[above] = 'ABOVE_SOURCE_DOMAIN'
        other_invalid = ~valid & inside & ~below & ~above
        reason[other_invalid] = sample.fallback_reason or 'OUTSIDE_SOURCE_DOMAIN'

        if self.below_domain_policy == 'ISA' and np.any(below):
            isa_pressure, isa_density, isa_temperature = vatmos(alt[below])
            temperature[below] = isa_temperature
            pressure[below] = isa_pressure
            density[below] = isa_density
            valid[below] = True
            source[below] = 'ISA'
            dataset_time[below] = ''
            reason[below] = f'CONFIGURED_BELOW_{self.source}_DOMAIN'

        sample = AtmosphereSample(
            temperature, pressure, density, valid,
            self._metadata(source), self._metadata(dataset_time), self._metadata(reason))
        invalid_count = int(np.count_nonzero(~valid))
        if invalid_count:
            invalid_reasons = np.asarray(reason, dtype=object)[~valid]
            for failure_reason in np.unique(invalid_reasons):
                count = int(np.count_nonzero(invalid_reasons == failure_reason))
                previous = self.failure_counts.get(failure_reason, 0)
                self.failure_counts[failure_reason] = previous + count
                if previous == 0:
                    stack.echo(
                        f'{self.source}: {count} invalid sample(s): {failure_reason}')
        if self.strict and not np.all(sample.valid):
            raise RuntimeError(f'{self.source} requested outside its validated domain')
        return sample

    def expire_time_slot(self, requested_slot):
        if self.expired_slot == requested_slot.isoformat():
            return
        reason = f'TIME_SLOT_EXPIRED:{self.active_slot}->{requested_slot.isoformat()}'
        stack.echo(f'{self.source}: automatic time update disabled; {reason}')
        self.clear(reason)
        self.expired_slot = requested_slot.isoformat()
        if self.strict:
            raise RuntimeError(f'{self.source} has no valid weather for the new time slot')

    def advance_time_slot(self, requested_slot, loader):
        """Load a new slot without ever retaining stale weather on failure."""
        try:
            return loader()
        except Exception as exc:
            reason = f'TIME_SLOT_UNAVAILABLE:{requested_slot.isoformat()}:{exc}'
            stack.echo(f'{self.source}: next time slot unavailable; using ISA: {reason}')
            self.clear(reason)
            if self.strict:
                raise RuntimeError(
                    f'{self.source} has no valid weather for {requested_slot.isoformat()}') from exc
            return False, reason

    @stack.command(name='METEOCONFIG')
    def configure(self, option: str = '', value: str = ''):
        """Inspect or set runtime meteorology policy for reproducible scenarios."""
        if not option:
            return True, (
                f'Meteorology: strict={self.strict} '
                f'below={self.below_domain_policy} '
                f'time_autoupdate={bool(bs.settings.meteo_time_autoupdate)} '
                f'time_interpolation={bool(bs.settings.meteo_time_interpolation)}')
        option = option.upper()
        if option == 'BELOW':
            try:
                self.below_domain_policy = self._validate_below_domain_policy(value)
            except (ValueError, NotImplementedError) as exc:
                return False, str(exc)
            bs.settings.meteo_below_domain_policy = self.below_domain_policy
            return self.configure()
        values = {'ON': True, 'TRUE': True, '1': True,
                  'OFF': False, 'FALSE': False, '0': False}
        if value.upper() not in values:
            return False, 'METEOCONFIG value must be ON or OFF'
        enabled = values[value.upper()]
        if option == 'STRICT':
            self.strict = enabled
        elif option == 'TIMEUPDATE':
            bs.settings.meteo_time_autoupdate = enabled
        elif option == 'INTERPOLATION':
            bs.settings.meteo_time_interpolation = enabled
            if not enabled:
                self.next_cube = None
        else:
            return False, 'METEOCONFIG option must be STRICT, BELOW, TIMEUPDATE, or INTERPOLATION'
        return self.configure()

    @stack.command(name='METEOSTATUS')
    def point_status(self, lat: 'lat', lon: 'lon', alt: 'alt'):
        """Show the loaded weather at one latitude, longitude, and altitude."""
        result = self._interpolate([lat], [lon], [alt], getattr(bs.sim, 'utc', None))
        if result is None:
            return False, f'{self.source}: no active weather dataset'
        north, east, _ = result
        sample = self.get_atmosphere([lat], [lon], [alt], getattr(bs.sim, 'utc', None))
        valid = bool(sample.valid[0])
        source = str(np.asarray(sample.source, dtype=object).flat[0])
        reason_value = str(np.asarray(sample.fallback_reason, dtype=object).flat[0])
        reason = reason_value or '-'
        if source == 'ISA':
            north[0] = east[0] = 0.0
        return True, (
            f'{self.source}: lat={lat:.6f} deg lon={lon:.6f} deg '
            f'alt={alt:.1f} m/{alt / 0.3048:.1f} ft '
            f'valid={valid} source={source} dataset={sample.dataset_time} fallback={reason}\n'
            f'  wind_north={north[0]:.3f} m/s wind_east={east[0]:.3f} m/s '
            f'T={sample.temperature[0]:.3f} K p={sample.pressure[0]:.3f} Pa '
            f'rho={sample.density[0]:.6f} kg/m3')

    def desired_slot(self, utc):
        return previous_slot(utc, self.slot_hours)
