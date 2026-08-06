"""Typed, per-aircraft BADA envelope policy and quality events."""

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Optional

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
    requested: Optional[float] = None
    applied: Optional[float] = None
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
