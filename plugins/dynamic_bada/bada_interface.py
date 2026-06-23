"""
dynamic_bada.bada_interface
============================
Unified adapter around pyBADA's ``Bada3Aircraft`` and ``Bada4Aircraft``.

Design principles
-----------------
* One ``BadaModelCache`` per BADA version holds all loaded model objects,
  keyed by aircraft type string.  Models are loaded lazily and shared
  across aircraft of the same type.
* ``BadaInterface`` wraps a single model (BADA3 or BADA4) and exposes a
  uniform API for forces, ROCD, fuel flow, and envelope.
* **No aerodynamic equations are duplicated here.**  Every computation
  is delegated to the corresponding pyBADA method.

BADA3 dummy model synonym resolution
--------------------------------------
The BADA3 DUMMY directory ships with a SYNONYM.NEW file that maps
ICAO type codes (A320, B738, …) to generic data files (J2M___, J2H___,
…).  ``BadaModelCache`` reads that file at startup and uses the mapping
to load the most appropriate generic model when an exact match fails.

BADA4 dummy model selection
-----------------------------
BADA4 DUMMY ships with four models differentiated by engine type:
  Dummy-TWIN      — twin-jet (default fallback)
  Dummy-TWIN-plus — twin-jet, higher mass
  Dummy-TBP       — turboprop
  Dummy-PST       — piston

The cache selects the appropriate fallback based on a rough ICAO engine
category heuristic when the exact model is not found.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import pyBADA.atmosphere as _atm

from .config import DynBadaConfig


# ═══════════════════════════════════════════════════════════════════════════════
# Atmosphere states and helpers (moved from atmosphere.py)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class AtmosState:
    """Grouped ISA atmospheric properties at a given altitude."""
    theta:   float   # temperature ratio  T/T0  [-]
    delta:   float   # pressure ratio     p/p0  [-]
    sigma:   float   # density ratio      rho/rho0  [-]
    T_K:     float   # static temperature [K]
    a_ms:    float   # speed of sound [m/s]
    rho:     float   # air density [kg/m³]
    p_Pa:    float   # static pressure [Pa]


def get_properties(alt_m: float, delta_temp: float = 0.0) -> AtmosState:
    """
    Return full ISA atmospheric state at *alt_m* (geopotential, metres)
    and temperature deviation *delta_temp* [K].

    Delegates entirely to ``pyBADA.atmosphere.atmosphereProperties``.
    """
    theta, delta, sigma_val = _atm.atmosphereProperties(h=alt_m, deltaTemp=delta_temp)

    # Derived quantities from pyBADA helpers
    T_K   = _atm.theta(h=alt_m, deltaTemp=delta_temp) * 288.15   # ISA T0
    a_ms  = _atm.aSound(theta=theta)
    rho   = sigma_val * 1.225   # ISA rho0 = 1.225 kg/m³
    p_Pa  = delta * 101325.0  # ISA p0 = 101325 Pa

    return AtmosState(
        theta=theta, delta=delta, sigma=sigma_val,
        T_K=T_K, a_ms=a_ms, rho=rho, p_Pa=p_Pa,
    )


def delta_temp_from_actual(T_actual_K: float, alt_m: float) -> float:
    """
    Compute ISA temperature deviation given an actual temperature measurement.
    Delegates to ``pyBADA.atmosphere.ISATemperatureDeviation``.
    """
    return _atm.ISATemperatureDeviation(
        temperature=T_actual_K, pressureAltitude=alt_m
    )


def tas2mach(tas: float, theta: float) -> float:
    """True airspeed [m/s] → Mach number.  Re-exported from pyBADA."""
    return _atm.tas2Mach(v=tas, theta=theta)


def mach2tas(mach: float, theta: float) -> float:
    """Mach number → True airspeed [m/s].  Re-exported from pyBADA."""
    return _atm.mach2Tas(M=mach, theta=theta)


def sigma(theta: float, delta: float) -> float:
    """Density ratio from theta and delta.  Re-exported from pyBADA."""
    return _atm.sigma(theta=theta, delta=delta)


# ═══════════════════════════════════════════════════════════════════════════════
# Result dataclasses
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class ForceResult:
    """Aerodynamic and thrust forces from one pyBADA query."""
    thrust:  float   # commanded / actual thrust [N]
    drag:    float   # total drag force [N]
    lift:    float   # lift force [N]
    T_max:   float   # max continuous thrust (MCMB) [N]
    T_idle:  float   # idle thrust (LIDL) [N], clamped ≥ 0
    T_mcrz:  float   # max cruise thrust (MCRZ) [N]
    CL:      float   # lift coefficient [-]
    CD:      float   # drag coefficient [-]
    M:       float   # Mach number [-]
    config:  Any     # flight configuration tag (opaque, passed back to ff)
    HLid:    Any     # high-lift device index (BADA4, None for BADA3)
    LG:      Any     # landing gear flag (BADA4, None for BADA3)


@dataclass(slots=True)
class ROCDResult:
    """Rate-of-climb/descent from the BADA energy equation."""
    rocd: float   # [m/s], positive = climb
    esf:  float   # energy share factor [-]


@dataclass(slots=True)
class FuelResult:
    """Fuel flow from one pyBADA query."""
    ff: float   # [kg/s], always ≥ minimum fuel flow


@dataclass(slots=True)
class EnvelopeResult:
    """Speed envelope limits from pyBADA."""
    vmin:   float   # minimum operating CAS [m/s]  (pyBADA VMin)
    vmax:   float   # maximum operating CAS [m/s]  (pyBADA VMax)
    vstall: float   # stall CAS [m/s]
    hmax:   float   # service ceiling [m]


# ═══════════════════════════════════════════════════════════════════════════════
# BADA3 synonym table reader
# ═══════════════════════════════════════════════════════════════════════════════

def _load_b3_synonym(bada3_dir: str) -> dict[str, str]:
    """
    Parse SYNONYM.NEW and return {ICAO_type: file_stem} mapping.
    Lines look like:  CD * A320   AIRBUS   A320-231   J2M___  Y
    """
    synonym: dict[str, str] = {}
    path = os.path.join(bada3_dir, "SYNONYM.NEW")
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                # Active synonym lines start with "CD * "
                if not line.startswith("CD *"):
                    continue
                # Strip "CD * " prefix then split
                parts = line[5:].split()
                if len(parts) >= 4:
                    icao = parts[0].upper()
                    file_stem = parts[3].upper()
                    synonym[icao] = file_stem
    except FileNotFoundError:
        pass
    return synonym


# ═══════════════════════════════════════════════════════════════════════════════
# BADA4 fallback heuristic
# ═══════════════════════════════════════════════════════════════════════════════

# Rough ICAO engine-category prefix → BADA4 dummy name
_B4_HEURISTIC: dict[str, str] = {
    # Propeller piston aircraft often have 4-letter codes starting with C/P/BE
    "C":    "Dummy-PST",    # Cessna family
    "PA":   "Dummy-PST",
    "BE":   "Dummy-PST",
    # Turboprops
    "AT":   "Dummy-TBP",
    "SF":   "Dummy-TBP",
    "DH":   "Dummy-TBP",
    "PL":   "Dummy-TBP",
    "SA":   "Dummy-TBP",
    "CN":   "Dummy-TBP",
}

def _b4_fallback_for(actype: str) -> str:
    """Return the most appropriate BADA4 dummy model name for *actype*."""
    upper = actype.upper()
    for prefix, model in _B4_HEURISTIC.items():
        if upper.startswith(prefix):
            return model
    return "Dummy-TWIN"  # default: twin-jet wide/narrow body


# ═══════════════════════════════════════════════════════════════════════════════
# Model caches (one per BADA generation, shared across all aircraft)
# ═══════════════════════════════════════════════════════════════════════════════

class _Bada4Cache:
    """Lazy-loading cache for BADA4 models."""

    def __init__(self, cfg: DynBadaConfig) -> None:
        self._cfg = cfg
        self._cache: dict[str, tuple[Optional[Any], bool]] = {}

    def get(self, actype: str) -> tuple[Optional[Any], bool]:
        """Return (Bada4Aircraft, is_dummy) for *actype*."""
        key = actype.upper()
        if key in self._cache:
            return self._cache[key]

        from pyBADA.bada4 import Bada4Aircraft
        bdir  = self._cfg.bada4_dir
        bver  = self._cfg.bada4_version
        fb    = _b4_fallback_for(actype)

        # Try: exact name, case-insensitive scan, chosen fallback, Dummy-TWIN
        names_to_try: list[str] = [key]
        try:
            entries = os.listdir(bdir)
            ci = next((e for e in entries if e.upper() == key and e != key), None)
            if ci:
                names_to_try.append(ci)
        except OSError:
            pass
        if fb not in names_to_try:
            names_to_try.append(fb)
        if "Dummy-TWIN" not in names_to_try:
            names_to_try.append("Dummy-TWIN")

        model = None
        is_dummy = True
        for name in names_to_try:
            try:
                model = Bada4Aircraft(badaVersion=bver, acName=name, filePath=bdir)
                is_dummy = (name != actype)
                label = actype if name == actype else f"{actype} → {name}"
                print(f"[dynamic_bada/B4] Loaded {label}")
                break
            except Exception:
                pass

        if model is None:
            print(f"[dynamic_bada/B4] No model found for {actype} — performance unavailable")
            self._cache[key] = (None, True)
            return None, True

        self._cache[key] = (model, is_dummy)
        return model, is_dummy


class _Bada3Cache:
    """Lazy-loading cache for BADA3 models, with synonym resolution."""

    def __init__(self, cfg: DynBadaConfig) -> None:
        self._cfg = cfg
        self._cache: dict[str, tuple[Optional[Any], bool]] = {}
        self._synonym: dict[str, str] = _load_b3_synonym(cfg.bada3_dir)

    def get(self, actype: str) -> tuple[Optional[Any], bool]:
        """Return (Bada3Aircraft, is_dummy) for *actype*."""
        key = actype.upper()
        if key in self._cache:
            return self._cache[key]

        from pyBADA.bada3 import Bada3Aircraft
        bdir = self._cfg.bada3_dir
        bver = self._cfg.bada3_version

        # Resolution order: exact key → synonym file → generic fallback
        names_to_try: list[str] = [key]
        if key in self._synonym:
            syn = self._synonym[key]
            if syn not in names_to_try:
                names_to_try.append(syn)
        # Generic large-jet fallback
        if "J2H___" not in names_to_try:
            names_to_try.append("J2H___")

        model = None
        is_dummy = True
        for name in names_to_try:
            try:
                model = Bada3Aircraft(badaVersion=bver, acName=name, filePath=bdir)
                resolved_name = getattr(model, 'acName', name).upper().strip('_')
                is_dummy = (resolved_name != key)
                label = actype if name == actype else f"{actype} → {name}"
                print(f"[dynamic_bada/B3] Loaded {label}")
                break
            except Exception:
                pass

        if model is None:
            print(f"[dynamic_bada/B3] No model found for {actype} — performance unavailable")
            self._cache[key] = (None, True)
            return None, True

        self._cache[key] = (model, is_dummy)
        return model, is_dummy


# ═══════════════════════════════════════════════════════════════════════════════
# Unified interface
# ═══════════════════════════════════════════════════════════════════════════════

class BadaInterface:
    """
    Uniform performance API wrapping either a BADA3 or BADA4 model.

    Parameters
    ----------
    model:
        A ``Bada3Aircraft`` or ``Bada4Aircraft`` instance.
    bada_version:
        Integer 3 or 4.
    is_dummy:
        Boolean indicating if this is a generic fallback/dummy model.

    All public methods accept plain Python scalars and return typed
    result dataclasses.  No numpy is used here — vectorisation happens
    one level up in ``DynamicAircraft`` / ``plugin.py``.
    """

    def __init__(self, model: Any, bada_version: int, is_dummy: bool = True) -> None:
        self._model = model
        self._ver   = bada_version
        self._is_dummy = is_dummy

    # ── Convenience properties ─────────────────────────────────────────────────

    @property
    def is_dummy(self) -> bool:
        """True if this is a generic/fallback model."""
        return self._is_dummy

    @property
    def mref(self) -> float:
        """Reference mass [kg]."""
        return self._model.AC.MREF

    @property
    def mmin(self) -> float:
        """Minimum operating mass [kg].

        BADA3 stores the OEW (minimum operating empty weight) in
        ``model.AC.mass["minimum"]``, NOT as an ``MMIN`` attribute.
        Using the wrong fallback (0.6 * MREF) produced a value that could be
        ABOVE a legitimately commanded mass (e.g. MASS AC 70000), causing
        ``new_mass = max(mass - ff*dt, mmin)`` to silently clamp the mass
        back up every tick — making the MASS command appear non-functional.
        """
        if self._ver == 3:
            try:
                return float(self._model.AC.mass["minimum"])
            except (TypeError, KeyError, AttributeError):
                pass
        return getattr(self._model.AC, "MMIN", self.mref * 0.6)

    # ── Internal atmosphere helpers ────────────────────────────────────────────

    def _atmos(self, alt_m: float, delta_temp: float) -> tuple:
        """Return (theta, delta, sigma, M=None) — M computed later."""
        theta = _atm.theta(h=alt_m, deltaTemp=delta_temp)
        delta = _atm.delta(h=alt_m, deltaTemp=delta_temp)
        sig   = _atm.sigma(theta=theta, delta=delta)
        return theta, delta, sig

    # ── Public API ─────────────────────────────────────────────────────────────

    def max_altitude(self, mass: float, delta_temp: float = 0.0) -> float:
        """Service ceiling [m] from pyBADA."""
        if self._ver == 4:
            # Need a reference config at cruise
            ref_h, ref_tas = 1000.0, 128.6   # ~250 kt
            theta, delta, _ = self._atmos(ref_h, delta_temp)
            M = tas2mach(ref_tas, theta)
            cfg_ = self._model.flightEnvelope.getConfig(
                phase="Cruise", h=ref_h, mass=mass, v=ref_tas, deltaTemp=delta_temp)
            HLid, LG = self._model.flightEnvelope.getAeroConfig(config=cfg_)
            return self._model.flightEnvelope.maxAltitude(
                HLid=HLid, LG=LG, M=M, deltaTemp=delta_temp, mass=mass)
        else:
            return self._model.flightEnvelope.maxAltitude(
                mass=mass, deltaTemp=delta_temp)

    def envelope(self,
                 alt_m: float, mass: float, tas: float,
                 atmos: AtmosState, delta_temp: float) -> EnvelopeResult:
        """Speed envelope at current state (pyBADA)."""
        theta, delta, sig = atmos.theta, atmos.delta, atmos.sigma
        M = tas2mach(max(tas, 1.0), theta)

        if self._ver == 4:
            cfg_ = self._model.flightEnvelope.getConfig(
                phase="Cruise", h=alt_m, mass=mass, v=tas, deltaTemp=delta_temp)
            HLid, LG = self._model.flightEnvelope.getAeroConfig(config=cfg_)
            vmin   = self._model.flightEnvelope.VMin(
                config=cfg_, theta=theta, delta=delta, mass=mass)
            vmax   = self._model.flightEnvelope.VMax(
                h=alt_m, HLid=HLid, LG=LG, delta=delta, theta=theta, mass=mass)
            vstall = self._model.flightEnvelope.VStall(
                mass=mass, HLid=HLid, LG=LG, theta=theta, delta=delta)
            hmax   = self.max_altitude(mass, delta_temp)
        else:
            cfg_ = self._model.flightEnvelope.getConfig(
                phase="Cruise", h=alt_m, mass=mass, v=tas, deltaTemp=delta_temp)
            vmin   = self._model.flightEnvelope.VMin(
                h=alt_m, mass=mass, config=cfg_, deltaTemp=delta_temp)
            vmax   = self._model.flightEnvelope.VMax(h=alt_m, deltaTemp=delta_temp)
            vstall = self._model.flightEnvelope.VStall(mass=mass, config=cfg_)
            hmax   = self.max_altitude(mass, delta_temp)

        # Sanitize None values to safe defaults
        vmin   = vmin if vmin is not None else 0.0
        vmax   = vmax if vmax is not None else 1e6
        vstall = vstall if vstall is not None else 0.0
        hmax   = hmax if hmax is not None else 1e6

        return EnvelopeResult(vmin=vmin, vmax=vmax, vstall=vstall, hmax=hmax)

    def forces(self,
               alt_m: float, mass: float, tas: float, ax: float,
               phase: str, atmos: AtmosState,
               delta_temp: float,
               load_n: float = 1.0) -> ForceResult:
        """
        Compute aerodynamic and thrust forces via pyBADA.

        Parameters
        ----------
        alt_m:      Altitude [m]
        mass:       Current mass [kg]
        tas:        True airspeed [m/s]
        ax:         Current longitudinal acceleration [m/s²]
        phase:      "Climb", "Cruise", or "Descent"
        atmos:      AtmosState at current altitude
        delta_temp: ISA temperature deviation [K]
        load_n:     Load factor n = 1/cos(φ).  Default 1.0 (wings level).
                    When > 1.0 (banked turn), CL is scaled by n so the drag
                    polar returns the correct turn-induced drag penalty, and
                    logged lift reflects actual lift = n·m·g.
        """
        tas = max(tas, 1.0)
        theta, delta, sig = atmos.theta, atmos.delta, atmos.sigma
        M = tas2mach(tas, theta)

        if self._ver == 4:
            return self._forces_b4(alt_m, mass, tas, ax, phase,
                                   delta_temp, theta, delta, sig, M,
                                   load_n=load_n)
        else:
            return self._forces_b3(alt_m, mass, tas, ax, phase,
                                   delta_temp, theta, delta, sig, M,
                                   load_n=load_n)

    def _forces_b4(self, alt_m, mass, tas, ax,
                   phase, delta_temp, theta, delta, sig, M,
                   load_n: float = 1.0) -> ForceResult:
        fe = self._model.flightEnvelope

        cfg_  = fe.getConfig(phase=phase, h=alt_m, mass=mass, v=tas, deltaTemp=delta_temp)
        HLid, LG = fe.getAeroConfig(config=cfg_)

        CL_level = fe.CL(delta=delta, mass=mass, M=M)
        # In a banked turn (load_n > 1) the aircraft needs CL_turn = n·CL_level
        # to produce the required lift L = n·m·g.  Feeding CL_turn into the
        # drag polar automatically captures the induced-drag penalty.
        CL_use = CL_level * load_n
        CD = fe.CD(HLid=HLid, LG=LG, CL=CL_use, M=M)
        D  = fe.D(delta=delta, M=M, CD=CD)
        L  = fe.L(delta=delta, M=M, CL=CL_use)   # logged lift = n·m·g in turns

        T_max  = fe.Thrust(delta=delta, theta=theta, M=M, deltaTemp=delta_temp, rating="MCMB")
        T_idle = max(fe.Thrust(delta=delta, theta=theta, M=M, deltaTemp=delta_temp, rating="LIDL"), 0.0)
        T_mcrz = fe.Thrust(delta=delta, theta=theta, M=M, deltaTemp=delta_temp, rating="MCRZ")

        # Phase-dependent thrust selection — mirrors pybadaperf.py exactly
        if phase == "Climb":
            # Full MCMB thrust during climb (same as pybadaperf.py BADA4 path)
            T = T_max
        elif phase == "Descent":
            T = T_idle
        else:  # Cruise
            # ax encodes the autopilot speed-error signal from the previous tick:
            #   ax < 0  → aircraft is too fast → reduce thrust below drag
            #   ax > 0  → aircraft is too slow → increase thrust above drag
            # Clamped to [T_idle, T_mcrz] to stay within operational limits.
            # (Identical to pybadaperf.py lines 1042-1044)
            import numpy as np
            T = float(np.clip(D + mass * ax, T_idle, T_mcrz))

        return ForceResult(
            thrust=T, drag=D, lift=L,
            T_max=T_max, T_idle=T_idle, T_mcrz=T_mcrz,
            CL=CL_use, CD=CD, M=M,
            config=cfg_, HLid=HLid, LG=LG,
        )

    def _forces_b3(self, alt_m, mass, tas, ax,
                   phase, delta_temp, theta, delta, sig, M,
                   load_n: float = 1.0) -> ForceResult:
        fe = self._model.flightEnvelope

        cfg_     = fe.getConfig(phase=phase, h=alt_m, mass=mass, v=tas, deltaTemp=delta_temp)
        CL_level = fe.CL(sigma=sig, mass=mass, tas=tas)
        # In a banked turn, scale CL by load factor for the drag polar.
        CL_use = CL_level * load_n
        CD = fe.CD(CL=CL_use, config=cfg_)
        D  = fe.D(sigma=sig, tas=tas, CD=CD)
        L  = fe.L(sigma=sig, tas=tas, CL=CL_use)  # logged lift = n·m·g in turns

        T_max  = self._model.Thrust(h=alt_m, deltaTemp=delta_temp, rating="MCMB", v=tas, config=cfg_)
        T_idle = self._model.Thrust(h=alt_m, deltaTemp=delta_temp, rating="LIDL", v=tas, config=cfg_)
        T_mcrz = self._model.Thrust(h=alt_m, deltaTemp=delta_temp, rating="MCRZ", v=tas, config=cfg_)

        if phase == "Climb":
            T = T_max
            # Uncomment the following two lines to enable the reduced power model for the climb phase:
            # Ccr = self._model.reducedPower(h=alt_m, mass=mass, deltaTemp=delta_temp)
            # T   = T_max * Ccr if Ccr is not None else T_max
        elif phase == "Descent":
            T_des = self._model.TDes(h=alt_m, deltaTemp=delta_temp, v=tas, config=cfg_)
            T = max(T_des, T_idle)
        else:  # Cruise
            # Include autopilot ax signal so speed errors are corrected.
            # (Identical to pybadaperf.py lines 589-591)
            import numpy as np
            T = float(np.clip(D + mass * ax, T_idle, T_mcrz))

        return ForceResult(
            thrust=T, drag=D, lift=L,
            T_max=T_max, T_idle=T_idle, T_mcrz=T_mcrz,
            CL=CL_use, CD=CD, M=M,
            config=cfg_, HLid=None, LG=None,
        )

    def rocd(self,
             alt_m: float, mass: float, tas: float,
             phase: str, force: ForceResult,
             atmos: AtmosState, delta_temp: float) -> ROCDResult:
        """Rate of climb/descent [m/s] from the BADA energy equation.

        pyBADA ROCD signature:
            ROCD(T[N], D[N], v[m/s], mass[kg], ESF[-], h[m], deltaTemp[K])
                → float  [m/s]

        The `vs` (current vertical speed) is NOT an input to pyBADA ROCD;
        ROCD is computed purely from the force balance.  The wrapper does not
        accept vs to make that explicit and avoid accidental reliance on it.
        """
        M     = force.M          # Mach [-]  from ForceResult
        theta = atmos.theta      # temperature ratio [-]

        if self._ver == 4:
            esf = self._model.flightEnvelope.esf(
                h=alt_m,              # altitude [m]
                deltaTemp=delta_temp, # ISA deviation [K]
                flightEvolution="constCAS",
                M=M,                  # Mach [-]
                phase=phase,
                v=tas,                # TAS [m/s]
                vdes=None)
            rocd_val = self._model.flightEnvelope.ROCD(
                T=force.thrust,   # thrust [N]
                D=force.drag,     # drag   [N]
                v=tas,            # TAS    [m/s]
                mass=mass,        # mass   [kg]
                ESF=esf,          # energy share factor [-]
                h=alt_m,          # altitude [m]
                deltaTemp=delta_temp)  # ISA deviation [K]  → returns [m/s]
        else:
            # BADA3: must pass M= explicitly.
            # Airplane.esf() in pyBADA/aircraft.py calls
            # checkArgument("M", **kwargs) for constCAS — missing it raises
            # TypeError: Missing M argument every tick, causing ROCD to
            # fall back to current VS in all modes.
            esf = self._model.esf(
                h=alt_m,              # altitude [m]
                deltaTemp=delta_temp, # ISA deviation [K]
                flightEvolution="constCAS",
                M=M,                  # Mach [-]
                phase=phase,
                v=tas,                # TAS [m/s]
                vdes=None)
            rocd_val = self._model.ROCD(
                T=force.thrust,   # thrust [N]
                D=force.drag,     # drag   [N]
                v=tas,            # TAS    [m/s]
                mass=mass,        # mass   [kg]
                ESF=esf,          # energy share factor [-]
                h=alt_m,          # altitude [m]
                deltaTemp=delta_temp)  # ISA deviation [K]  → returns [m/s]

        return ROCDResult(rocd=rocd_val, esf=esf)

    def fuelflow(self,
                 alt_m: float, tas: float, mass: float,
                 phase: str, force: ForceResult,
                 atmos: AtmosState, delta_temp: float) -> FuelResult:
        """Fuel flow [kg/s] from pyBADA."""
        M     = force.M
        theta = atmos.theta
        delta = atmos.delta

        if self._ver == 4:
            fe   = self._model.flightEnvelope
            cfg_ = force.config
            if phase in ("Climb", "Descent"):
                rating = "MCMB" if phase == "Climb" else "LIDL"
                ff = fe.ff(delta=delta, theta=theta, deltaTemp=delta_temp,
                           rating=rating, M=M, config=cfg_)
            else:
                CT = fe.CT(delta=delta, Thrust=force.thrust)
                ff = fe.ff(delta=delta, theta=theta, deltaTemp=delta_temp,
                           CT=CT, M=M, config=cfg_)
            ff_idle = fe.ff(delta=delta, theta=theta, deltaTemp=delta_temp,
                            rating="LIDL", M=M, config=cfg_)
            ff = max(ff, ff_idle, 0.0)
        else:
            cfg_   = force.config
            ff     = self._model.ff(h=alt_m, v=tas, T=force.thrust,
                                    config=cfg_, flightPhase=phase)
            ff_min = self._model.ffMin(h=alt_m)
            ff     = max(ff, ff_min, 0.0)

        return FuelResult(ff=ff)

    def thrust_bounds(self,
                      alt_m: float, tas: float,
                      atmos: AtmosState, delta_temp: float) -> tuple:
        """Return (T_max_MCMB, T_idle_LIDL) [N] without phase selection.

        Used as a lightweight fallback when ``forces()`` raises an exception
        so that T_max_arr / T_idle_arr are still populated for saveheader.
        """
        tas = max(tas, 1.0)
        theta, delta, _sig = atmos.theta, atmos.delta, atmos.sigma
        M = tas2mach(tas, theta)

        try:
            if self._ver == 4:
                fe     = self._model.flightEnvelope
                T_max  = fe.Thrust(delta=delta, theta=theta, M=M,
                                   deltaTemp=delta_temp, rating="MCMB")
                T_idle = max(fe.Thrust(delta=delta, theta=theta, M=M,
                                       deltaTemp=delta_temp, rating="LIDL"), 0.0)
            else:
                cfg_ = self._model.flightEnvelope.getConfig(
                    phase="Cruise", h=alt_m, mass=self.mref, v=tas,
                    deltaTemp=delta_temp)
                T_max  = self._model.Thrust(h=alt_m, deltaTemp=delta_temp,
                                            rating="MCMB", v=tas, config=cfg_)
                T_idle = self._model.Thrust(h=alt_m, deltaTemp=delta_temp,
                                            rating="LIDL", v=tas, config=cfg_)
        except Exception:
            T_max = T_idle = 0.0

        return T_max, T_idle


# ═══════════════════════════════════════════════════════════════════════════════
# Factory helpers used by plugin.py
# ═══════════════════════════════════════════════════════════════════════════════

def make_caches(cfg: DynBadaConfig) -> tuple[_Bada4Cache, _Bada3Cache]:
    """Create and return one cache per BADA generation."""
    return _Bada4Cache(cfg), _Bada3Cache(cfg)


def make_interface(actype: str,
                   bada_version: int,
                   b4_cache: _Bada4Cache,
                   b3_cache: _Bada3Cache) -> Optional[BadaInterface]:
    """
    Return a ``BadaInterface`` for *actype* using *bada_version* (3 or 4),
    or ``None`` if no model could be loaded.
    """
    if bada_version == 4:
        res = b4_cache.get(actype)
    else:
        res = b3_cache.get(actype)

    if res is None:
        return None
    model, is_dummy = res
    if model is None:
        return None
    return BadaInterface(model, bada_version, is_dummy)
