"""
energy/performance_model.py

``IPerformanceModel`` is the Strategy interface that decouples the rest of
the plugin (GuidanceLayer) from which BADA family is active.
The ``PERFMODEL BADA3|BADA4`` stack command swaps the concrete adapter
behind this interface at runtime; no other module needs to be modified.

The interface is intentionally coarse-grained: one ``compute()`` call per
aircraft per simulation tick.  This allows each adapter to evaluate the
shared atmosphere terms (theta, delta, deltaTemp, Mach) only once per call
rather than repeatedly inside separate thrust/drag/fuel-flow accessors.

``BadaPerformanceModelMixin`` collects the shared helpers (atmosphere
computation, ESF wrapper, array growth) that both BADA 3 and BADA 4 adapters
need, avoiding near-duplicate code between them.
"""

import math
from abc import ABC, abstractmethod

import numpy as np
import pyBADA.atmosphere as atm

from ..state import EnergyTerms, FlightEnvelope


class IPerformanceModel(ABC):
    """Strategy interface implemented by Bada3PerformanceAdapter and
    Bada4PerformanceAdapter.

    All methods are indexed by the aircraft's position in BlueSky's traf
    array (``idx``).  The adapter maintains its own per-aircraft state
    (model references, dummy flags) at the same index.
    """

    BADA_VER: str = "?"   # BADA version string, e.g. "3.15" or "4.2"
    BADA_DIR: str = "?"   # Absolute path to the BADA data directory

    @abstractmethod
    def create(self, n: int):
        """Allocate model state for `n` newly created aircraft.

        Mirrors BlueSky's traf.create(n) semantics: `n` aircraft are
        always appended at the end of the existing array.
        """

    @abstractmethod
    def has_model(self, idx: int) -> bool:
        """Return True if this aircraft has type-specific BADA data loaded.

        Returns False if the aircraft is using a generic fallback (dummy)
        model because its ICAO type code could not be resolved to real BADA
        data.  bridge.py uses this to log a warning at creation time.
        """

    @abstractmethod
    def initial_mass_kg(self, idx: int) -> float:
        """Return the BADA reference mass (MREF) for this aircraft [kg].

        Used by bridge.py to seed AircraftState.mass_kg on creation so the
        aircraft starts at a physically reasonable operating weight.
        """

    @abstractmethod
    def get_envelope(self, idx: int, alt_m: float, tas_ms: float,
                      mass_kg: float, temp_actual_k: float,
                      p_pa: float = None) -> FlightEnvelope:
        """Return the current performance envelope for one aircraft.

        Called every tick for all aircraft by PyBada3DOFPerf.update() to
        refresh the vmin/vmax/hmax arrays used by BlueSky's ASAS and by
        FeasibilityFilter.
        """
        ...

    @abstractmethod
    def compute(self, idx: int, alt_m: float, tas_ms: float, mass_kg: float,
                temp_actual_k: float, ax_ms2: float, bada_phase: str,
                flight_evolution: str, p_pa: float = None) -> EnergyTerms:
        """Core TEM query for one aircraft — the main performance call.

        :param idx:              Aircraft index in the adapter's arrays.
        :param alt_m:            Current altitude [m] (geometric or pressure).
        :param tas_ms:           Current True Airspeed [m/s].
        :param mass_kg:          Current aircraft mass [kg].
        :param temp_actual_k:    Actual static air temperature [K].
        :param ax_ms2:           Previous-tick longitudinal acceleration [m/s²],
                                 used only during cruise to compute the
                                 drag-balanced thrust T = clip(D + m*ax, ...).
        :param bada_phase:       "cl" (climb) / "des" (descent) / None (cruise).
                                 Drives thrust rating selection (MCMB/LIDL/MCRZ)
                                 and pyBADA's aerodynamic configuration lookup.
        :param flight_evolution: "constCAS" / "constM" / "constTAS" / "acc" / "dec".
                                 Selects the ESF branch in pyBADA's Airplane.esf().
                                 This is the *only* parameter that changes the ESF;
                                 thrust, drag, and fuel flow are unaffected by it.
        :param p_pa:             Actual static pressure [Pa] at the aircraft's
                                 position.  When provided, the adapter computes
                                 ISA pressure altitude from it (hp = f(p)) and
                                 passes that to ISATemperatureDeviation, which is
                                 what pyBADA expects.  Passing geometric alt_m
                                 instead gives the wrong deltaTemp and corrupts all
                                 downstream BADA computations.  Defaults to None
                                 (falls back to alt_m) for call-sites without
                                 pressure data.
        :returns: EnergyTerms with thrust, drag, fuel flow, ESF, and ROCD.
        """


class BadaPerformanceModelMixin:
    """Shared, BADA-family-agnostic helpers mixed into both adapters.

    Both Bada3PerformanceAdapter and Bada4PerformanceAdapter inherit this
    mixin to share atmosphere computation, the ESF wrapper, and the NumPy
    array growth utility without code duplication.
    """

    @staticmethod
    def _sanitize_inputs(alt_m: float, tas_ms: float, mass_kg: float,
                         temp_actual_k: float) -> None:
        """Raise ValueError if any core input is NaN, inf, or physically impossible.

        Called at the top of compute() and get_envelope() in both adapters so
        that corrupted BlueSky state values produce a clear diagnostic instead
        of the opaque numpy 'Array must not contain infs or NaNs' error.
        """
        checks = {
            "tas_ms":       tas_ms,
            "alt_m":        alt_m,
            "mass_kg":      mass_kg,
            "temp_actual_k": temp_actual_k,
        }
        for name, val in checks.items():
            if not math.isfinite(val):
                raise ValueError(
                    f"non-finite input {name}={val!r} — "
                    f"state: alt={alt_m:.1f}m tas={tas_ms:.2f}m/s "
                    f"mass={mass_kg:.1f}kg T={temp_actual_k:.2f}K"
                )
        if tas_ms <= 0:
            raise ValueError(
                f"non-positive TAS={tas_ms:.2f}m/s — state: alt={alt_m:.1f}m mass={mass_kg:.1f}kg"
            )
        if not (-500.0 <= alt_m <= 15_000.0):
            raise ValueError(
                f"altitude out of bounds alt_m={alt_m:.1f}m (valid: -500..15000 m, ~FL492) — "
                f"tas={tas_ms:.2f}m/s mass={mass_kg:.1f}kg"
            )
        if mass_kg <= 0:
            raise ValueError(
                f"non-positive mass={mass_kg:.1f}kg — state: alt={alt_m:.1f}m tas={tas_ms:.2f}m/s"
            )

    def _atmosphere(self, alt_m: float, tas_ms: float, temp_actual_k: float,
                    p_pa: float = None):
        """Compute dimensionless ISA atmospheric ratios and Mach number.

        Returns (theta, delta, deltaTemp, mach):
            theta    = T / T0            (temperature ratio)
            delta    = P / P0            (pressure ratio)
            deltaTemp= T_actual - T_ISA  (ISA temperature deviation [K])
            mach     = TAS / a           (Mach number)

        :param p_pa: Actual static pressure [Pa].  When provided, the ISA
            pressure altitude hp_m is computed from it:
                hp_m = (1 - (p / 101325)^0.190263) * 44330.77
            and passed to ISATemperatureDeviation.  This is the physically
            correct input for pyBADA: it expects pressure altitude, not
            geometric altitude.  Using geometric altitude instead would give
            a wrong deltaTemp and corrupt all downstream calculations.
            Defaults to None (uses alt_m as fallback when pressure is unavailable).
        """
        if p_pa is not None and p_pa > 0:
            # Pressure altitude from actual static pressure [m]
            hp_m = (1.0 - (p_pa / 101325.0) ** 0.190263) * 44330.77
        else:
            hp_m = alt_m   # fallback: no meteo data available
        deltaTemp = atm.ISATemperatureDeviation(
            temperature=temp_actual_k, pressureAltitude=hp_m
        )
        theta = atm.theta(h=hp_m, deltaTemp=deltaTemp)
        delta = atm.delta(h=hp_m, deltaTemp=deltaTemp)
        mach  = atm.tas2Mach(v=tas_ms, theta=theta)
        return theta, delta, deltaTemp, mach

    @staticmethod
    def _grow(arr: np.ndarray, n: int, fill=0.0, dtype=None):
        """Append `n` elements initialised to `fill` to the NumPy array `arr`.

        Used by both adapters' create() to grow per-aircraft state arrays
        in sync with BlueSky's traf.create(n) call.
        """
        extra = np.full(n, fill, dtype=dtype) if dtype is not None else np.zeros(n)
        return np.concatenate([arr, extra])

    def _select_esf(self, ac, alt_m, mach, deltaTemp, bada_phase, flight_evolution):
        """Thin wrapper around pyBADA's Airplane.esf (BADA 4 instance method).

        Calls ``ac.esf(**kwargs)`` with the correct keyword arguments so that
        callers never need to deal with pyBADA's somewhat awkward **kwargs
        signature.  pyBADA requires h, M, and deltaTemp for every
        flight_evolution mode (including constTAS and acc/dec), so they are
        always passed.

        For BADA 3, Bada3PerformanceAdapter overrides this method to call the
        static ``Airplane.esf()`` class method instead, because BADA 3 OPF
        objects do not populate the instance esf() method (which is an XML-only
        attribute of BADA 4 objects).
        """
        kwargs = dict(h=alt_m, M=mach, deltaTemp=deltaTemp,
                      flightEvolution=flight_evolution)
        if flight_evolution in ("acc", "dec") and bada_phase is not None:
            # pyBADA needs the phase string ("cl" / "des") to select the
            # correct acc/dec ESF branch (0.3 or 1.7).
            kwargs["phase"] = bada_phase
        return ac.esf(**kwargs)
