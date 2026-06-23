"""
dynamic_bada.flight_dynamics
=============================
Pure-function flight physics — **no BlueSky imports, no pyBADA imports**.

All functions take plain Python scalars and return scalars.
They implement the kinematic / geometric relationships that sit between
pyBADA force outputs and state derivatives, following standard 3-DOF
point-mass flight-mechanics for transport aircraft.

References
----------
- EUROCONTROL BADA 4 User Manual, §5 (point-mass model)
- Stevens, Lewis, Johnson — Aircraft Control and Simulation, 3rd ed.
"""
from __future__ import annotations

import math

# ── Physical constants ──────────────────────────────────────────────────────────
G0: float = 9.80665   # standard gravity [m/s²]
EARTH_RADIUS: float = 6_371_000.0   # [m]


# ═══════════════════════════════════════════════════════════════════════════════
# Kinematics
# ═══════════════════════════════════════════════════════════════════════════════

def flight_path_angle(vs: float, tas: float, min_tas: float = 1.0) -> float:
    """
    Instantaneous flight-path angle γ [rad].

    γ = arcsin(VS / TAS)   (small-angle version used in BADA)

    Parameters
    ----------
    vs:      vertical speed [m/s], positive = climb
    tas:     true airspeed [m/s]
    min_tas: guard against near-zero TAS [m/s]
    """
    return math.asin(max(-1.0, min(1.0, vs / max(tas, min_tas))))


# ═══════════════════════════════════════════════════════════════════════════════
# Longitudinal force balance
# ═══════════════════════════════════════════════════════════════════════════════

def longitudinal_acceleration(thrust: float, drag: float,
                               weight: float, gamma_rad: float,
                               mass: float) -> float:
    """
    Net longitudinal acceleration [m/s²].

    ax = (T - D - W · sin γ) / m

    This is the horizontal (along-track) component of Newton's second law
    for a point-mass in a quasi-steady flight.  Used to update TAS.

    Parameters
    ----------
    thrust:    thrust force [N]
    drag:      drag force [N]
    weight:    weight force W = m · g₀ [N]
    gamma_rad: flight-path angle [rad]
    mass:      aircraft mass [kg]
    """
    return (thrust - drag - weight * math.sin(gamma_rad)) / mass


def required_thrust_cruise(drag: float, mass: float, ax: float) -> float:
    """
    Required thrust for cruise acceleration/deceleration [N].

    T_req = D + m · ax   (level flight, γ ≈ 0)
    """
    return drag + mass * ax


# ═══════════════════════════════════════════════════════════════════════════════
# Angle wrapping utilities
# ═══════════════════════════════════════════════════════════════════════════════

def wrap_angle_180(angle_deg: float) -> float:
    """Normalise heading/bearing to (-180, +180]."""
    return (angle_deg + 180.0) % 360.0 - 180.0


def wrap_angle_360(angle_deg: float) -> float:
    """Normalise heading to [0, 360)."""
    return angle_deg % 360.0
