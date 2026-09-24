"""TEM energy-allocation policies that share one contract.

Every policy distributes the same specific excess power between the
along-track acceleration ``a`` and the geometric vertical rate ``w``::

    a + (g0 / V) * w = (Thr - D) / m,    idle <= Thr <= maximum

within a vertical interval ``[w_lo, w_hi]`` supplied by the caller (altitude
capture and envelope limits) and, optionally, ``a >= a_floor``. The policies
differ only in their objective:

* ``SPEED_PRIORITY``: acceleration closest to the request, then the vertical
  rate closest to the reference.
* ``VERTICAL_PRIORITY``: vertical rate closest to the reference, then the
  acceleration closest to the request.
* ``JOINT``: minimise ``w_a (a - a_req)^2 + w_w (w - w_ref)^2``; the two
  priority policies are its limits for a vanishing weight.

The guidance capability of a policy is the acceleration it delivers for the
native request of plus or minus ``axmax``, so guidance plans with the same
equations that later fly the aircraft.
"""

from dataclasses import dataclass
from enum import Enum
from itertools import combinations

import numpy as np

from bluesky.tools.aero import g0
from .model import EvaluationError, _clamp_thrust, allocate_speed_priority


class AllocationPolicy(str, Enum):
    SPEED = 'SPEED_PRIORITY'
    VERTICAL = 'VERTICAL_PRIORITY'
    JOINT = 'JOINT'


def parse_allocation_policy(value):
    if isinstance(value, AllocationPolicy):
        return value
    text = str(value).upper().strip()
    aliases = {'SPEED': AllocationPolicy.SPEED, 'VERTICAL': AllocationPolicy.VERTICAL}
    try:
        return aliases.get(text) or AllocationPolicy(text)
    except ValueError as exc:
        raise ValueError(f'unknown allocation policy {value!r}; '
                         'expected SPEED, VERTICAL, or JOINT') from exc


@dataclass(frozen=True)
class JointWeights:
    """Weights on the squared acceleration [m/s2] and vertical-rate [m/s] errors."""

    acceleration: float
    vertical: float

    def validate(self):
        values = (self.acceleration, self.vertical)
        if not all(np.isfinite(values)) or min(values) <= 0.0:
            raise EvaluationError('JOINT weights must be finite and positive')
        return self


@dataclass(frozen=True)
class Allocation:
    thrust: float
    acceleration: float
    vertical_rate: float
    required_thrust: float
    limited: bool
    reason: str


def _finish(tas, mass, drag, idle_thrust, maximum_thrust, requested_acceleration,
            acceleration, vertical_rate):
    """Thrust for the chosen point; limitation reports the unmet request."""
    k = g0 / tas
    required = drag + mass * (requested_acceleration + k * vertical_rate)
    _, limited, reason, _, _ = _clamp_thrust(required, idle_thrust, maximum_thrust)
    thrust, _, _, _, _ = _clamp_thrust(drag + mass * (acceleration + k * vertical_rate),
                                       idle_thrust, maximum_thrust)
    return Allocation(thrust, (thrust - drag) / mass - k * vertical_rate,
                      float(vertical_rate), float(required), limited, reason)


def _band(tas, mass, drag, idle_thrust, maximum_thrust):
    idle = idle_thrust if np.isfinite(idle_thrust) else -np.inf
    return g0 / tas, (idle - drag) / mass, (maximum_thrust - drag) / mass


def _floor_limit(k, high_e, minimum_vertical_rate, maximum_vertical_rate, floor):
    """Highest vertical rate that still leaves ``a >= floor`` at maximum thrust."""
    if not np.isfinite(floor):
        return maximum_vertical_rate, floor
    limit = min(maximum_vertical_rate, (high_e - floor) / k)
    if limit < minimum_vertical_rate:
        # The floor is unreachable: fly the lowest permitted rate and give all
        # remaining power to acceleration, to regain the floor soonest.
        return minimum_vertical_rate, high_e - k * minimum_vertical_rate
    return limit, floor


def _vertical_priority(tas, mass, drag, idle_thrust, maximum_thrust, requested_acceleration,
                       reference, low_w, high_w, floor):
    k, low_e, high_e = _band(tas, mass, drag, idle_thrust, maximum_thrust)
    high_w, floor = _floor_limit(k, high_e, low_w, high_w, floor)
    vertical = float(np.clip(reference, low_w, high_w))
    acceleration = float(np.clip(requested_acceleration,
                                 max(low_e - k * vertical, floor), high_e - k * vertical))
    return _finish(tas, mass, drag, idle_thrust, maximum_thrust, requested_acceleration,
                   acceleration, vertical)


def _joint(tas, mass, drag, idle_thrust, maximum_thrust, requested_acceleration,
           reference, low_w, high_w, floor, weights):
    """Exact minimiser of the weighted quadratic over the feasible polygon.

    The optimum of a convex quadratic over a polygon is the unconstrained
    minimiser, the minimiser on one boundary line, or a vertex; all are
    enumerated and the cheapest feasible one is chosen.
    """
    weights = weights.validate()
    k, low_e, high_e = _band(tas, mass, drag, idle_thrust, maximum_thrust)
    high_w, floor = _floor_limit(k, high_e, low_w, high_w, floor)
    # Lines n . (a, w) = c bounding the region.
    lines = [((0.0, 1.0), low_w), ((0.0, 1.0), high_w), ((1.0, k), high_e)]
    if np.isfinite(low_e):
        lines.append(((1.0, k), low_e))
    if np.isfinite(floor):
        lines.append(((1.0, 0.0), floor))
    scale = 1e-9 * max(1.0, abs(high_e), abs(low_w), abs(high_w))

    def feasible(a, w):
        e = a + k * w
        return (low_w - scale <= w <= high_w + scale and e <= high_e + scale
                and (not np.isfinite(low_e) or e >= low_e - scale)
                and (not np.isfinite(floor) or a >= floor - scale))

    def cost(a, w):
        return (weights.acceleration * (a - requested_acceleration) ** 2 +
                weights.vertical * (w - reference) ** 2)

    candidates = [(requested_acceleration, reference)]
    for (n1, n2), c in lines:
        # Minimise the cost on n1*a + n2*w = c via its Lagrange conditions.
        wa, ww = weights.acceleration, weights.vertical
        denominator = n1 * n1 / wa + n2 * n2 / ww
        lam = (n1 * requested_acceleration + n2 * reference - c) / denominator
        candidates.append((requested_acceleration - lam * n1 / wa, reference - lam * n2 / ww))
    for ((a1, b1), c1), ((a2, b2), c2) in combinations(lines, 2):
        det = a1 * b2 - a2 * b1
        if abs(det) > 1e-12:
            candidates.append(((c1 * b2 - c2 * b1) / det, (a1 * c2 - a2 * c1) / det))
    feasible_points = [point for point in candidates if feasible(*point)]
    if not feasible_points:
        raise EvaluationError('JOINT allocation has no feasible point')
    acceleration, vertical = min(feasible_points, key=lambda point: cost(*point))
    vertical = float(np.clip(vertical, low_w, high_w))
    return _finish(tas, mass, drag, idle_thrust, maximum_thrust, requested_acceleration,
                   acceleration, vertical)


def allocate_energy(policy, *, tas, mass, drag, idle_thrust, maximum_thrust,
                    requested_acceleration, reference_vertical_rate,
                    minimum_vertical_rate, maximum_vertical_rate,
                    minimum_acceleration=-np.inf, weights=None):
    """Allocate specific excess power under one policy; see the module docstring."""
    policy = parse_allocation_policy(policy)
    values = (tas, mass, drag, maximum_thrust, requested_acceleration,
              reference_vertical_rate, minimum_vertical_rate, maximum_vertical_rate)
    if not np.all(np.isfinite(values)) or tas <= 0 or mass <= 0:
        raise EvaluationError('energy allocation requires finite state and bounds')
    if minimum_vertical_rate > maximum_vertical_rate:
        raise EvaluationError('empty vertical interval for energy allocation')
    _clamp_thrust(drag, idle_thrust, maximum_thrust)
    if policy == AllocationPolicy.SPEED:
        thrust, acceleration, vertical, required, limited, reason = allocate_speed_priority(
            tas=tas, mass=mass, drag=drag, idle_thrust=idle_thrust,
            maximum_thrust=maximum_thrust, requested_acceleration=requested_acceleration,
            preferred_vertical_rate=reference_vertical_rate,
            minimum_vertical_rate=minimum_vertical_rate,
            maximum_vertical_rate=maximum_vertical_rate)
        return Allocation(thrust, acceleration, vertical, required, limited, reason)
    if policy == AllocationPolicy.VERTICAL:
        return _vertical_priority(
            tas, mass, drag, idle_thrust, maximum_thrust, requested_acceleration,
            float(np.clip(reference_vertical_rate, minimum_vertical_rate, maximum_vertical_rate)),
            minimum_vertical_rate, maximum_vertical_rate, minimum_acceleration)
    if weights is None:
        raise EvaluationError('JOINT allocation requires explicit weights')
    # The reference may lie outside the interval (e.g. the guidance rate near an
    # altitude capture); the optimum then sits on the interval bound.
    return _joint(tas, mass, drag, idle_thrust, maximum_thrust, requested_acceleration,
                  float(reference_vertical_rate), minimum_vertical_rate, maximum_vertical_rate,
                  minimum_acceleration, weights)


def acceleration_capability(policy, request_magnitude, **state):
    """Accelerations a policy delivers for the native requests +/- ``request_magnitude``.

    ``state`` holds the ``allocate_energy`` keywords other than the request.
    Returns the signed accelerations for an acceleration and a deceleration.
    """
    magnitude = abs(float(request_magnitude))
    return tuple(allocate_energy(policy, requested_acceleration=sign * magnitude,
                                 **state).acceleration for sign in (1.0, -1.0))
