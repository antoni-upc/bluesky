"""Typed, per-aircraft BADA envelope policy and quality events."""

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Optional

import numpy as np

from bluesky.core.signal import Signal


class EnvelopePolicy(str, Enum):
    OFF = 'OFF'
    REPORT = 'REPORT'
    ENFORCE = 'ENFORCE'
    ABORT = 'ABORT'


class EnvelopeProfile(str, Enum):
    CORE_ONLY = 'CORE_ONLY'
    LONGITUDINAL = 'LONGITUDINAL'
    FULL = 'FULL'
    CUSTOM = 'CUSTOM'


class EnvelopeCheck(str, Enum):
    MASS_MIN = 'MASS_MIN'
    MASS_MAX = 'MASS_MAX'
    LOW_SPEED = 'LOW_SPEED'
    HIGH_SPEED = 'HIGH_SPEED'
    MACH_MIN = 'MACH_MIN'
    MACH_MAX = 'MACH_MAX'
    ALTITUDE_MAX = 'ALTITUDE_MAX'
    ROC_MAX = 'ROC_MAX'
    ROD_MAX = 'ROD_MAX'
    BANK_ANGLE = 'BANK_ANGLE'
    LOAD_FACTOR = 'LOAD_FACTOR'


LONGITUDINAL_CHECKS = tuple(check for check in EnvelopeCheck
                            if check not in (EnvelopeCheck.BANK_ANGLE,
                                             EnvelopeCheck.LOAD_FACTOR))
PROFILE_CHECKS = {
    EnvelopeProfile.CORE_ONLY: (),
    EnvelopeProfile.LONGITUDINAL: LONGITUDINAL_CHECKS,
    EnvelopeProfile.FULL: tuple(EnvelopeCheck),
}


class EnvelopeStatus(str, Enum):
    VALID = 'VALID'
    INFEASIBLE = 'INFEASIBLE'
    UNKNOWN = 'UNKNOWN'


class EnvelopeAction(str, Enum):
    NONE = 'NONE'
    ACCEPTED = 'ACCEPTED'
    REJECTED = 'REJECTED'
    LIMITED = 'LIMITED'
    ABORTED = 'ABORTED'


@dataclass(frozen=True)
class MassBounds:
    minimum: Optional[float]
    maximum: Optional[float]
    reason: str = ''

    @property
    def known(self):
        return not self.reason


@dataclass(frozen=True)
class FlightBounds:
    configuration: str
    minimum_cas: Optional[float]
    maximum_cas: Optional[float]
    minimum_mach: Optional[float]
    maximum_mach: Optional[float]
    maximum_altitude: Optional[float]
    minimum_tas: Optional[float] = None
    maximum_tas: Optional[float] = None
    reason: str = ''


@dataclass(frozen=True)
class VerticalBounds:
    minimum_rocd: Optional[float]
    maximum_rocd: Optional[float]
    reason: str = ''


@dataclass(frozen=True)
class LateralBounds:
    configuration: str
    minimum_load_factor: Optional[float]
    maximum_load_factor: Optional[float]
    maximum_bank_angle_deg: Optional[float]
    reason: str = ''


@dataclass(frozen=True)
class EnvelopeResult:
    status: EnvelopeStatus
    failed_checks: tuple[EnvelopeCheck, ...] = ()
    reason: str = ''


@dataclass(frozen=True)
class QualityEvent:
    aircraft: str
    component: str
    reason: str
    policy: str
    action: str
    continuation: str
    requested: Optional[Any] = None
    applied: Optional[Any] = None
    sim_time_s: Optional[float] = None

    def as_dict(self):
        return asdict(self)


quality_events = Signal('PYBADA_QUALITY_EVENT')


def parse_policy(value):
    if isinstance(value, EnvelopePolicy):
        return value
    try:
        return EnvelopePolicy(str(value).upper().strip())
    except ValueError as exc:
        raise ValueError('policy must be OFF, REPORT, ENFORCE, or ABORT') from exc


def parse_profile(value):
    if isinstance(value, EnvelopeProfile):
        return value
    try:
        return EnvelopeProfile(str(value).upper().strip())
    except ValueError as exc:
        raise ValueError('profile must be CORE_ONLY, LONGITUDINAL, FULL, or CUSTOM') from exc


def parse_checks(values):
    result = []
    for value in values:
        if isinstance(value, EnvelopeCheck):
            tokens = (value.value,)
        else:
            tokens = str(value).replace(',', ' ').split()
        for token in tokens:
            try:
                check = EnvelopeCheck(token.upper())
            except ValueError as exc:
                raise ValueError(f'unknown envelope check {token!r}') from exc
            if check in result:
                raise ValueError(f'duplicate envelope check {check.value}')
            result.append(check)
    return tuple(result)


def expand_checks(profile, explicit=()):
    profile = parse_profile(profile)
    if profile == EnvelopeProfile.CUSTOM:
        return tuple(explicit)
    return PROFILE_CHECKS[profile]


def _finite_attr(model, names):
    """Find a scalar on either the adapter or wrapped pyBADA object."""
    objects = (model, getattr(model, 'model', None),
               getattr(model, 'aircraft', None), getattr(model, 'AC', None))
    for obj in objects:
        if obj is None:
            continue
        for name in names:
            value = getattr(obj, name, None)
            try:
                scalar = float(value)
            except (TypeError, ValueError):
                continue
            if np.isfinite(scalar):
                return scalar
    return None


def mass_bounds(model):
    """Normalize documented BADA 3/4 operating-empty and maximum masses."""
    minimum = _finite_attr(model, ('OEW', 'oew', 'M_MIN', 'm_min'))
    maximum = _finite_attr(model, ('MTOW', 'mtow', 'M_MAX', 'm_max'))
    if minimum is None or maximum is None:
        return MassBounds(minimum, maximum, 'missing or non-finite OEW/MTOW')
    if minimum <= 0.0 or maximum <= 0.0 or minimum > maximum:
        return MassBounds(minimum, maximum, 'contradictory OEW/MTOW')
    return MassBounds(minimum, maximum)


def evaluate_mass(value, bounds, checks):
    try:
        mass = float(value)
    except (TypeError, ValueError):
        return EnvelopeResult(EnvelopeStatus.UNKNOWN, reason='mass is not numeric')
    if not np.isfinite(mass) or mass <= 0.0:
        return EnvelopeResult(EnvelopeStatus.UNKNOWN, reason='mass must be finite and positive')
    selected = set(checks) & {EnvelopeCheck.MASS_MIN, EnvelopeCheck.MASS_MAX}
    if not selected:
        return EnvelopeResult(EnvelopeStatus.VALID)
    if not bounds.known:
        return EnvelopeResult(EnvelopeStatus.UNKNOWN, reason=bounds.reason)
    failed = []
    if EnvelopeCheck.MASS_MIN in selected and mass < bounds.minimum:
        failed.append(EnvelopeCheck.MASS_MIN)
    if EnvelopeCheck.MASS_MAX in selected and mass > bounds.maximum:
        failed.append(EnvelopeCheck.MASS_MAX)
    reason = ','.join(check.value for check in failed)
    return EnvelopeResult(EnvelopeStatus.INFEASIBLE if failed else EnvelopeStatus.VALID,
                          tuple(failed), reason)


def evaluate_flight(cas, mach, altitude, bounds, checks):
    """Evaluate longitudinal state against operating-point BADA bounds."""
    values = np.asarray((cas, mach, altitude), dtype=float)
    if not np.all(np.isfinite(values)) or cas < 0.0 or mach < 0.0:
        return EnvelopeResult(
            EnvelopeStatus.UNKNOWN,
            reason='CAS/Mach/altitude must be finite and speeds non-negative')
    selected = set(checks)
    required = {
        EnvelopeCheck.LOW_SPEED: bounds.minimum_cas,
        EnvelopeCheck.HIGH_SPEED: bounds.maximum_cas,
        EnvelopeCheck.MACH_MIN: bounds.minimum_mach,
        EnvelopeCheck.MACH_MAX: bounds.maximum_mach,
        EnvelopeCheck.ALTITUDE_MAX: bounds.maximum_altitude,
    }
    missing = [check.value for check, value in required.items()
               if check in selected and (value is None or not np.isfinite(value))]
    if missing:
        detail = bounds.reason or f'missing bounds for {",".join(missing)}'
        return EnvelopeResult(EnvelopeStatus.UNKNOWN, reason=detail)
    contradictory = ((EnvelopeCheck.LOW_SPEED in selected and
                      EnvelopeCheck.HIGH_SPEED in selected and
                      bounds.minimum_cas > bounds.maximum_cas) or
                     (EnvelopeCheck.MACH_MIN in selected and
                      EnvelopeCheck.MACH_MAX in selected and
                      bounds.minimum_mach > bounds.maximum_mach))
    if contradictory:
        return EnvelopeResult(EnvelopeStatus.UNKNOWN,
                              reason='contradictory speed envelope bounds')
    failed = []
    if EnvelopeCheck.LOW_SPEED in selected and cas < bounds.minimum_cas:
        failed.append(EnvelopeCheck.LOW_SPEED)
    if EnvelopeCheck.HIGH_SPEED in selected and cas > bounds.maximum_cas:
        failed.append(EnvelopeCheck.HIGH_SPEED)
    if EnvelopeCheck.MACH_MIN in selected and mach < bounds.minimum_mach:
        failed.append(EnvelopeCheck.MACH_MIN)
    if EnvelopeCheck.MACH_MAX in selected and mach > bounds.maximum_mach:
        failed.append(EnvelopeCheck.MACH_MAX)
    if EnvelopeCheck.ALTITUDE_MAX in selected and altitude > bounds.maximum_altitude:
        failed.append(EnvelopeCheck.ALTITUDE_MAX)
    return EnvelopeResult(
        EnvelopeStatus.INFEASIBLE if failed else EnvelopeStatus.VALID,
        tuple(failed), ','.join(check.value for check in failed))


def evaluate_vertical(vertical_rate, bounds, checks):
    """Evaluate signed ROCD: climb is positive and descent is negative."""
    try:
        vertical_rate = float(vertical_rate)
    except (TypeError, ValueError):
        return EnvelopeResult(EnvelopeStatus.UNKNOWN,
                              reason='vertical rate is not numeric')
    if not np.isfinite(vertical_rate):
        return EnvelopeResult(EnvelopeStatus.UNKNOWN,
                              reason='vertical rate must be finite')
    selected = set(checks) & {EnvelopeCheck.ROC_MAX, EnvelopeCheck.ROD_MAX}
    if not selected:
        return EnvelopeResult(EnvelopeStatus.VALID)
    required = {
        EnvelopeCheck.ROD_MAX: bounds.minimum_rocd,
        EnvelopeCheck.ROC_MAX: bounds.maximum_rocd,
    }
    missing = [check.value for check, value in required.items()
               if check in selected and (value is None or not np.isfinite(value))]
    if missing:
        return EnvelopeResult(
            EnvelopeStatus.UNKNOWN,
            reason=bounds.reason or f'missing bounds for {",".join(missing)}')
    if (bounds.minimum_rocd is not None and bounds.maximum_rocd is not None and
            bounds.minimum_rocd > bounds.maximum_rocd):
        return EnvelopeResult(EnvelopeStatus.UNKNOWN,
                              reason='contradictory vertical-rate envelope bounds')
    failed = []
    # TEM output and the bound are evaluated independently. Treat differences
    # below one centimetre per second as numerical/operating-point noise.
    tolerance = 0.01
    if (EnvelopeCheck.ROC_MAX in selected and
            vertical_rate > bounds.maximum_rocd + tolerance):
        failed.append(EnvelopeCheck.ROC_MAX)
    if (EnvelopeCheck.ROD_MAX in selected and
            vertical_rate < bounds.minimum_rocd - tolerance):
        failed.append(EnvelopeCheck.ROD_MAX)
    return EnvelopeResult(
        EnvelopeStatus.INFEASIBLE if failed else EnvelopeStatus.VALID,
        tuple(failed), ','.join(check.value for check in failed))


def evaluate_lateral(bank_angle_deg, load_factor, bounds, checks):
    try:
        bank = abs(float(bank_angle_deg))
        load = float(load_factor)
    except (TypeError, ValueError):
        return EnvelopeResult(EnvelopeStatus.UNKNOWN,
                              reason='bank angle/load factor is not numeric')
    if not np.all(np.isfinite((bank, load))):
        return EnvelopeResult(EnvelopeStatus.UNKNOWN,
                              reason='bank angle/load factor must be finite')
    selected = set(checks) & {EnvelopeCheck.BANK_ANGLE, EnvelopeCheck.LOAD_FACTOR}
    if not selected:
        return EnvelopeResult(EnvelopeStatus.VALID)
    required = {EnvelopeCheck.BANK_ANGLE: bounds.maximum_bank_angle_deg,
                EnvelopeCheck.LOAD_FACTOR: bounds.maximum_load_factor}
    missing = [check.value for check, value in required.items()
               if check in selected and (value is None or not np.isfinite(value))]
    if missing:
        return EnvelopeResult(EnvelopeStatus.UNKNOWN,
                              reason=bounds.reason or
                              f'missing bounds for {",".join(missing)}')
    failed = []
    if (EnvelopeCheck.BANK_ANGLE in selected and
            bank > bounds.maximum_bank_angle_deg + 0.01):
        failed.append(EnvelopeCheck.BANK_ANGLE)
    if (EnvelopeCheck.LOAD_FACTOR in selected and
            (load > bounds.maximum_load_factor + 0.001 or
             (bounds.minimum_load_factor is not None and
              load < bounds.minimum_load_factor - 0.001))):
        failed.append(EnvelopeCheck.LOAD_FACTOR)
    return EnvelopeResult(EnvelopeStatus.INFEASIBLE if failed else EnvelopeStatus.VALID,
                          tuple(failed), ','.join(check.value for check in failed))


def combine_results(*results):
    """Combine independently evaluated components without losing reasons."""
    unknown = next((result for result in results
                    if result.status == EnvelopeStatus.UNKNOWN), None)
    if unknown is not None:
        return unknown
    failed = tuple(dict.fromkeys(check for result in results
                                 for check in result.failed_checks))
    return EnvelopeResult(EnvelopeStatus.INFEASIBLE if failed else EnvelopeStatus.VALID,
                          failed, ','.join(check.value for check in failed))
