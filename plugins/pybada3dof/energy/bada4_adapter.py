"""
energy/bada4_adapter.py

Concrete Strategy adapter wrapping pyBADA's Bada4Aircraft (XML format).

Structurally identical to Bada3PerformanceAdapter — the only real differences
are the BADA 4 XML API for aerodynamics and thrust (all via
ac.flightEnvelope.*) and BADA 4's ROCD() signature, which has no
``reducedPower`` argument unlike BADA 3's.

ESF is computed by the inherited BadaPerformanceModelMixin._select_esf(),
which calls the instance method ac.esf(**kwargs).  Unlike BADA 3, the BADA 4
XML object does populate the instance esf() method, so no override is needed.

Fallback strategy for unknown aircraft types:
  1. Prefix match: scan all model subdirectories in BADA_DIR and pick the one
     that shares the longest common leading substring with the requested ICAO
     type.  E.g. 'A320' matches 'A320-200' before 'A330-200'.  Ties broken by
     alphabetical order of the candidate name.
  2. Generic DUMMY: if no prefix match is found (zero common characters),
     _load_fallback() first tries Dummy-TWIN (preferred — best stand-in for
     commercial narrowbody), then falls back to the first loadable directory in
     BADA_DIR sorted alphabetically.  The loaded slot is flagged is_dummy=True.
"""

import os
from pathlib import Path

import numpy as np

from pyBADA.bada4 import Bada4Aircraft
import pyBADA.atmosphere as _atm

from ..state import EnergyTerms, FlightEnvelope
from .performance_model import BadaPerformanceModelMixin, IPerformanceModel


class Bada4PerformanceAdapter(BadaPerformanceModelMixin, IPerformanceModel):

    BADA_VER = "4.2"
    # Path to the BADA 4 XML data directory.
    # pyBADA ships a DUMMY subset under its own package directory; update
    # BADA_DIR to point to a licensed BADA 4.x dataset for real-aircraft types.
    BADA_DIR = str(
        Path(__file__).parent.parent.parent.parent  # repo root
        / ".venv" / "lib" / "python3.12" / "site-packages"
        / "pyBADA" / "aircraft" / "BADA4" / "DUMMY"
    )

    def __init__(self, actype_lookup):
        """
        :param actype_lookup: callable(idx) -> ICAO type string.
            This adapter never imports bs.traf directly; all BlueSky coupling
            is handled by bridge.py through this lookup callable.
        """
        self._actype_lookup = actype_lookup
        self._cached_models = {}       # {actype_str: (Bada4Aircraft, is_dummy)}
        self._failed_models = set()    # types for which loading failed entirely
        self.model_refs = np.empty(0, dtype=object)  # per-aircraft model references
        self.is_dummy   = np.ones(0, dtype=bool)     # True if model is a fallback

    # ------------------------------------------------------------------
    def _available_model_dirs(self):
        """Return the sorted list of model subdirectory names in BADA_DIR."""
        try:
            return sorted([
                d for d in os.listdir(self.BADA_DIR)
                if os.path.isdir(os.path.join(self.BADA_DIR, d))
            ])
        except OSError:
            return []

    # ------------------------------------------------------------------
    def _find_best_match(self, actype: str, candidates: list) -> str | None:
        """Return the model directory name that best matches *actype* by longest
        common prefix.

        Compares *actype* (upper-cased) against every entry in *candidates*.
        The candidate with the most leading characters in common with *actype*
        wins; ties are broken by the alphabetical order already present in
        *candidates* (pass a sorted list).  Returns None when no candidate
        shares even a single leading character.
        """
        actype_up = actype.upper()
        best_name, best_len = None, 0
        for name in candidates:
            name_up = name.upper()
            common = 0
            for a, b in zip(actype_up, name_up):
                if a == b:
                    common += 1
                else:
                    break
            if common > best_len:
                best_len, best_name = common, name
        return best_name if best_len > 0 else None

    # ------------------------------------------------------------------
    def _load_fallback(self):
        """Load a generic BADA 4 Dummy model as a last-resort fallback.

        Only called when no prefix match was found in BADA_DIR.  Tries
        Dummy-TWIN first (preferred — best stand-in for commercial
        narrowbody), then falls back to the first loadable directory
        alphabetically.  Returns None if the directory is completely empty
        or all candidates raise exceptions.
        """
        all_dirs = self._available_model_dirs()
        # Prefer Dummy-TWIN; fall back to alphabetical order
        preferred = [n for n in all_dirs if n.upper() == "DUMMY-TWIN"]
        names     = preferred + [n for n in all_dirs if n.upper() != "DUMMY-TWIN"]
        for name in names:
            try:
                m = Bada4Aircraft(badaVersion=self.BADA_VER, acName=name,
                                  filePath=self.BADA_DIR)
                print(f"[Bada4Adapter] Using generic DUMMY fallback: '{name}'")
                return m
            except Exception:
                continue
        return None

    def create(self, n: int):
        self.model_refs = self._grow(self.model_refs, n, fill=None, dtype=object)
        self.is_dummy   = self._grow(self.is_dummy,   n, fill=True, dtype=bool)

        start = len(self.model_refs) - n
        for i in range(start, start + n):
            actype = self._actype_lookup(i).upper()

            if actype not in self._cached_models and actype not in self._failed_models:
                # --- 1. Try exact match ------------------------------------------
                try:
                    model = Bada4Aircraft(
                        badaVersion=self.BADA_VER, acName=actype, filePath=self.BADA_DIR,
                    )
                    resolved_name = getattr(model, "acName", actype).upper().strip("_")
                    is_fallback   = resolved_name != actype
                    if is_fallback:
                        print(f"[Bada4Adapter] WARNING: {actype} not found - using generic "
                              f"DUMMY aircraft '{resolved_name}'. Envelope limits relaxed. "
                              f"Install licensed BADA 4 data for accurate results.")
                    self._cached_models[actype] = (model, is_fallback)
                except Exception:
                    # --- 2. Try best-prefix match among available model dirs -------
                    all_dirs = self._available_model_dirs()
                    best = self._find_best_match(actype, all_dirs)
                    if best is not None:
                        try:
                            model = Bada4Aircraft(
                                badaVersion=self.BADA_VER, acName=best,
                                filePath=self.BADA_DIR,
                            )
                            print(f"[Bada4Adapter] '{actype}' not found - "
                                  f"using best prefix match: '{best}'")
                            self._cached_models[actype] = (model, True)
                        except Exception as exc:
                            print(f"[Bada4Adapter] Prefix match '{best}' for '{actype}' "
                                  f"failed to load: {exc}")
                            best = None   # fall through to generic dummy

                    if best is None:
                        # --- 3. Last resort: generic DUMMY model ------------------
                        print(f"[Bada4Adapter] No prefix match for '{actype}' - "
                              f"falling back to generic DUMMY model.")
                        self._failed_models.add(actype)
                        fallback = self._load_fallback()
                        self._cached_models[actype] = (fallback, True)

            ac, is_dummy = self._cached_models.get(actype, (None, True))
            self.model_refs[i] = ac
            self.is_dummy[i]   = is_dummy

    def has_model(self, idx: int) -> bool:
        return self.model_refs[idx] is not None and not self.is_dummy[idx]

    def initial_mass_kg(self, idx: int) -> float:
        ac = self.model_refs[idx]
        # ac.MREF is the BADA 4 XML reference mass [kg]
        return float(ac.MREF) if ac is not None else 60000.0

    # ------------------------------------------------------------------
    def crossover_altitude_m(self, idx: int) -> float:
        """Return the CAS/Mach crossover altitude for this aircraft [m].

        BADA 4 XML objects expose a ``speedSchedule`` attribute that contains
        the climb CAS2 [m/s] and Mach number used for the ICAO speed schedule.
        The crossover altitude is where that CAS equals that Mach number in TAS.

        Computation:
            speed_schedule = ac.flightEnvelope.getSpeedSchedule(phase='Climb')
            → returns [CAS1_ms, CAS2_ms, M]  (CAS in m/s, M dimensionless)
            crossover_m = atm.crossOver(cas=CAS2_ms, Mach=M)

        Falls back to 9,144 m (~FL300) if the speed schedule is unavailable
        (e.g. for a generic dummy without type-specific data or a pyBADA API
        change).
        """
        ac = self.model_refs[idx]
        if ac is None:
            return 9144.0
        try:
            # getSpeedSchedule returns [CAS1_ms, CAS2_ms, Mach] for the
            # requested phase.  CAS2 is the upper-altitude climb CAS that
            # transitions to the Mach schedule above the crossover altitude.
            sched = ac.flightEnvelope.getSpeedSchedule(phase="Climb")
            cas2_ms = float(sched[1])   # CAS2 [m/s]
            mach    = float(sched[2])   # Mach number [-]
            return float(_atm.crossOver(cas=cas2_ms, Mach=mach))
        except Exception:
            return 9144.0   # ~FL300 fallback when speed schedule is unavailable

    # ------------------------------------------------------------------
    def get_envelope(self, idx, alt_m, tas_ms, mass_kg, temp_actual_k,
                     p_pa: float = None) -> FlightEnvelope:
        ac = self.model_refs[idx]
        if ac is None:
            return FlightEnvelope(0, 1e6, 0, -1, -100, 100, 2.0, 0, 0, is_dummy=True)

        try:
            self._sanitize_inputs(alt_m, tas_ms, mass_kg, temp_actual_k)
            theta, delta, deltaTemp, M = self._atmosphere(alt_m, tas_ms, temp_actual_k, p_pa)
            if theta == 0.0:
                raise ValueError(f"theta=0 at alt={alt_m:.1f}m — atmosphere degenerate")
            sigma = delta / theta
            # getConfig() expects CAS [m/s], not TAS — convert
            cas_ms = _atm.tas2Cas(tas=tas_ms, delta=delta, sigma=sigma)
            # Must resolve config/HLid/LG before calling maxAltitude
            config = ac.flightEnvelope.getConfig(
                phase="Cruise", h=alt_m, mass=mass_kg, v=cas_ms, deltaTemp=deltaTemp
            )
            HLid, LG = ac.flightEnvelope.getAeroConfig(config=config)
            hmax = ac.flightEnvelope.maxAltitude(
                HLid=HLid, LG=LG, M=M, deltaTemp=deltaTemp, mass=mass_kg
            )
            # NOTE: VMin/VMax/VStall in pyBADA BADA 4 return CAS [m/s], NOT TAS.
            # They MUST be converted to TAS before being stored in FlightEnvelope
            # because FeasibilityFilter compares them against the aircraft's TAS.
            # Failing to convert causes the aircraft to be capped at ~263 kt TAS
            # instead of ~425 kt cruise TAS at FL370, producing excessive drag and
            # a permanently negative ROCD.
            vmin_cas  = ac.flightEnvelope.VMin(config=config, theta=theta, delta=delta,
                                                mass=mass_kg)
            vmax_cas  = ac.flightEnvelope.VMax(h=alt_m, HLid=HLid, LG=LG, delta=delta,
                                                theta=theta, mass=mass_kg)
            vstall_cas = ac.flightEnvelope.VStall(
                mass=mass_kg, HLid=HLid, LG=LG, theta=theta, delta=delta
            )
            # Convert CAS -> TAS for each speed limit
            vmin   = _atm.cas2Tas(cas=vmin_cas,   delta=delta, sigma=sigma)
            vmax   = _atm.cas2Tas(cas=vmax_cas,   delta=delta, sigma=sigma) \
                     if vmax_cas is not None else 1e6
            vstall = _atm.cas2Tas(cas=vstall_cas, delta=delta, sigma=sigma)
        except Exception as exc:
            print(f"[Bada4Adapter] Envelope query failed: {exc}")
            hmax, vmin, vmax, vstall = -1, 0, 1e6, 0

        return FlightEnvelope(
            vmin_ms=vmin, vmax_ms=vmax, vstall_ms=vstall, hmax_m=hmax,
            vsmin_ms=-6000 * 0.00508, vsmax_ms=6000 * 0.00508, axmax_ms2=2.0,
            thrust_max_n=0.0, thrust_idle_n=0.0, is_dummy=self.is_dummy[idx],
        )

    # ------------------------------------------------------------------
    def compute(self, idx, alt_m, tas_ms, mass_kg, temp_actual_k, ax_ms2,
                bada_phase, flight_evolution, p_pa: float = None) -> EnergyTerms:
        """Compute TEM energy terms for one BADA 4 aircraft.

        All BADA 4 XML calls go through ac.flightEnvelope.* (not top-level
        ac.* methods as in BADA 3).  ROCD() has no reducedPower argument
        (unlike BADA 3's ROCD signature).

        Altitude usage:
          hp_m   — ISA pressure altitude derived from p_pa (or alt_m fallback).
                   Used for all pyBADA atmospheric computations (Thrust, ff,
                   ESF, ROCD) because pyBADA internally recomputes theta/delta
                   from the altitude argument; passing pressure altitude ensures
                   that internal recomputation is consistent with our own theta
                   and delta values.
          alt_m  — Geometric MSL altitude.  Used ONLY for getConfig(), which
                   evaluates AGL phase-transition thresholds (HmaxTO, HmaxIC,
                   etc.) and therefore needs geometric, not pressure, altitude.
        """
        ac = self.model_refs[idx]
        if ac is None:
            return EnergyTerms(0, 0, 0, 0, 0, "CR", 0, 0)

        self._sanitize_inputs(alt_m, tas_ms, mass_kg, temp_actual_k)

        theta, delta, deltaTemp, M = self._atmosphere(alt_m, tas_ms, temp_actual_k, p_pa)
        sigma = delta / theta

        # Derive pressure altitude explicitly so it can be passed to pyBADA
        # calls that use altitude for internal atmosphere recomputation.
        if p_pa is not None and p_pa > 0:
            hp_m = (1.0 - (p_pa / 101325.0) ** 0.190263) * 44330.77
        else:
            hp_m = alt_m

        bs_phase = {"cl": "Climb", "des": "Descent"}.get(bada_phase, "Cruise")

        # getConfig() uses altitude for AGL phase thresholds (HmaxTO, HmaxIC, …)
        # → must receive geometric altitude, NOT pressure altitude.
        cas_ms = _atm.tas2Cas(tas=tas_ms, delta=delta, sigma=sigma)
        try:
            config = ac.flightEnvelope.getConfig(
                phase=bs_phase, h=alt_m, mass=mass_kg, v=cas_ms, deltaTemp=deltaTemp
            )
        except TypeError:
            # CAS outside all configuration speed boundaries — fall back to clean.
            config = "CR"

        HLid, LG = ac.flightEnvelope.getAeroConfig(config=config)

        # --- Aerodynamics (delta/M-based, no altitude argument) ----------
        CL = ac.flightEnvelope.CL(delta=delta, mass=mass_kg, M=M)
        CD = ac.flightEnvelope.CD(HLid=HLid, LG=LG, CL=CL, M=M)
        D  = ac.flightEnvelope.D(delta=delta, M=M, CD=CD)

        # --- Thrust (uses pressure altitude hp_m internally via theta/delta)
        # pyBADA's Thrust() accepts (delta, **kwargs) and only uses delta and
        # the polynomial coefficients in CT; theta and deltaTemp are passed
        # through to CT_nonLIDL for the kink-point computation.  All calls
        # here use the consistent (theta, delta, M, deltaTemp) derived from hp_m.
        T_max  = ac.flightEnvelope.Thrust(delta=delta, theta=theta, M=M,
                                           deltaTemp=deltaTemp, rating="MCMB")
        # NOTE: do NOT clamp T_idle to >= 0 here.  pyBADA BADA 4's LIDL thrust
        # is the physically correct net propulsive force at idle, which can be
        # legitimately near-zero or slightly negative at high altitude / high
        # Mach (net aerodynamic deceleration at flight idle).  Clamping to 0
        # makes T = 0 for the entire descent (T = T_idle in the des branch),
        # which zeroes the ROCD numerator and produces an excessively steep
        # descent driven by drag alone (Bug #1 — confirmed by data analysis).
        T_idle = float(np.nan_to_num(
            ac.flightEnvelope.Thrust(delta=delta, theta=theta, M=M,
                                     deltaTemp=deltaTemp, rating="LIDL"), nan=0.0))

        if bada_phase == "cl":
            # Climb: full MCMB thrust.
            T, ff_rating, CT = T_max, "MCMB", None
        elif bada_phase == "des":
            # Descent: idle thrust (LIDL).
            T, ff_rating, CT = T_idle, "LIDL", None
        else:
            # Cruise: drag-balanced thrust clamped to [T_idle, T_mcrz].
            T_mcrz = ac.flightEnvelope.Thrust(delta=delta, theta=theta, M=M,
                                               deltaTemp=deltaTemp, rating="MCRZ")
            T      = float(np.clip(D + mass_kg * ax_ms2, T_idle, T_mcrz))
            CT     = ac.flightEnvelope.CT(delta=delta, Thrust=T)
            ff_rating = None

        # --- Fuel flow --------------------------------------------------
        # pyBADA ff() accepts (delta, theta, deltaTemp, **kwargs) — config is
        # silently absorbed by **kwargs and not used by CF; it is kept for
        # forwards compatibility should pyBADA add config-dependent fuel flow.
        if ff_rating is not None:
            ff = ac.flightEnvelope.ff(delta=delta, theta=theta, deltaTemp=deltaTemp,
                                       rating=ff_rating, M=M)
        else:
            ff = ac.flightEnvelope.ff(delta=delta, theta=theta, deltaTemp=deltaTemp,
                                       CT=CT, M=M)
        # Clamp to idle fuel flow floor (CT=0 path can return sub-idle values).
        ff_idle = ac.flightEnvelope.ff(delta=delta, theta=theta, deltaTemp=deltaTemp,
                                        rating="LIDL", M=M)
        ff = max(ff, ff_idle)

        # --- ESF --------------------------------------------------------
        # Pass hp_m (pressure altitude) to ac.esf() so that the constCAS and
        # constM ESF formulas use the same pressure-based altitude as the rest
        # of our atmosphere model.  _select_esf() already accepts alt_m as a
        # positional arg; override it with hp_m here.
        esf = self._select_esf(ac, hp_m, M, deltaTemp, bada_phase, flight_evolution)
        if esf is None or (isinstance(esf, float) and np.isnan(esf)):
            esf = 0.3 if bada_phase in ("cl", "des") else 1.0

        # --- ROCD -------------------------------------------------------
        # Pass hp_m so pyBADA's internal theta = atm.theta(h=h) is consistent
        # with the theta we computed above.  Using geometric alt_m would give
        # a slightly different theta inside ROCD, corrupting the temperature
        # correction factor ((temp - deltaTemp) / temp).
        rocd = ac.ROCD(T=T, D=D, v=tas_ms, mass=mass_kg, ESF=esf,
                       h=hp_m, deltaTemp=deltaTemp)

        return EnergyTerms(
            thrust_n=T, drag_n=D, fuel_flow_kgps=ff, esf=esf, rocd_ms=rocd,
            config=config, thrust_max_n=T_max, thrust_idle_n=T_idle,
        )
