"""Atmosphere-provider data contract used by optional weather plugins."""

from dataclasses import dataclass

import numpy as np

from bluesky.tools.aero import R, T0, beta, gamma, gamma2, p0


@dataclass(frozen=True)
class AtmosphereSample:
    """Vectorized atmospheric state returned for the requested positions."""

    temperature: np.ndarray
    pressure: np.ndarray
    density: np.ndarray
    valid: np.ndarray
    source: str
    dataset_time: str = ""
    fallback_reason: str = ""


def pressure_altitude(pressure):
    """Return ISA pressure altitude [m] for static pressure [Pa]."""
    pressure = np.asarray(pressure, dtype=float)
    tropopause_p = p0 * (216.65 / T0) ** (-9.80665 / (beta * R))
    troposphere = (T0 / -beta) * (1.0 - (pressure / p0) ** (-beta * R / 9.80665))
    stratosphere = 11000.0 - (R * 216.65 / 9.80665) * np.log(pressure / tropopause_p)
    return np.where(pressure >= tropopause_p, troposphere, stratosphere)


def tas_to_mach(tas, temperature):
    """Convert TAS [m/s] to Mach using the applied temperature."""
    return np.asarray(tas) / np.sqrt(gamma * R * np.asarray(temperature))


def mach_to_cas(mach, pressure):
    """Convert Mach/static pressure to calibrated airspeed [m/s]."""
    mach = np.asarray(mach)
    pressure = np.asarray(pressure)
    qc = pressure * ((1.0 + 0.2 * mach * mach) ** gamma2 - 1.0)
    return np.sqrt(5.0 * gamma * R * T0 * ((qc / p0 + 1.0) ** (1.0 / gamma2) - 1.0))

