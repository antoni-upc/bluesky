"""
energy/bada3_adapter.py

Concrete Strategy adapter wrapping pyBADA's Bada3Aircraft (OPF/APF format).

The BADA 3 OPF API differs from the BADA 4 XML API in several important ways:
  - Aerodynamics: flightEnvelope.CL(sigma, mass, tas) / CD(CL, config) / D(sigma, tas, CD)
  - Thrust:       ac.Thrust(h, deltaTemp, rating, v, config)  [top-level, NOT flightEnvelope]
  - Fuel flow:    ac.ff(h, v, T, config, flightPhase)         [top-level, NOT flightEnvelope]
  - ROCD:         ac.ROCD(T, D, v, mass, ESF, h, deltaTemp)   [positional args]
  - ESF:          Airplane.esf(flightEvolution, h, M, deltaTemp) [STATIC class method]

Using the BADA 4 XML API (flightEnvelope.CL(delta, M), flightEnvelope.Thrust(...),
ac.esf()) on a BADA 3 OPF object silently fails because 'aeroConfig' and the
instance 'esf' method are XML-only attributes not populated by the OPF parser.
This is why Bada3PerformanceAdapter overrides _select_esf() to call the static
Airplane.esf() class method directly.

Fallback strategy for unknown aircraft types:
  BADA 3 dummy data ships as flat OPF/APF files (J2M___.OPF, J4H___.OPF, etc.)
  inside the DUMMY sub-directory of the BADA 3 data folder.  When the requested
  ICAO type is not found, _load_fallback() tries the following preference-ordered
  list and uses the first one available:
      J2M___ (medium twin-jet)  -> preferred, best stand-in for narrow-body
      J2H___ (heavy twin-jet)
      J4H___ (heavy quad-jet)
      BZJT__ (generic business jet)
      TP2M__ (twin turboprop)
      GA____ (general aviation)
"""

import numpy as np
import math

from pyBADA.bada3 import Bada3Aircraft
from pyBADA.aircraft import Airplane
import pyBADA.atmosphere as atm

from ..state import EnergyTerms, FlightEnvelope
from .performance_model import BadaPerformanceModelMixin, IPerformanceModel


class Bada3PerformanceAdapter(BadaPerformanceModelMixin, IPerformanceModel):

    BADA_VER = "3.15"
    BADA_DIR = "/home/paucr/bluesky/.venv/lib/python3.12/site-packages/pyBADA/aircraft/BADA3/DUMMY"

    # OPF-format DUMMY names tried in order of suitability for generic
    # commercial twin-jet scenarios.  The list is exhausted until a loadable
    # entry is found.
    _OPF_FALLBACK_NAMES = ["J2M___", "J2H___", "J4H___", "BZJT__", "TP2M__", "GA____"]

    def __init__(self, actype_lookup):
        """
        :param actype_lookup: callable(idx) -> ICAO type string.
            This adapter never imports bs.traf directly; all BlueSky coupling
            is handled by bridge.py through this lookup callable.
        """
        self._actype_lookup = actype_lookup
        self._cached_models = {}        # {actype_str: (Bada3Aircraft, is_dummy)}
        self._failed_models = set()     # types for which all loading attempts failed
        self.model_refs = np.empty(0, dtype=object)  # per-aircraft model references
        self.is_dummy   = np.ones(0, dtype=bool)     # True if model is a fallback

    # ------------------------------------------------------------------
    def _load_fallback(self):
        """Load the best available BADA 3 OPF dummy model as a fallback.

        BADA 3 dummy data ships as flat OPF/APF files (e.g. J2M___.OPF) in
        the BADA_DIR.  pyBADA's Bada3Aircraft loads them by file stem (acName).
        We try the preference-ordered list and return the first one that loads.
        Returns None if the directory is empty or all candidates fail.
        """
        import os
        try:
            available = set(
                os.path.splitext(f)[0]
                for f in os.listdir(self.BADA_DIR)
                if f.upper().endswith(".OPF")
            )
        except OSError:
            available = set()

        for name in self._OPF_FALLBACK_NAMES:
            if name not in available:
                continue
            try:
                m = Bada3Aircraft(badaVersion=self.BADA_VER, acName=name,
                                  filePath=self.BADA_DIR)
                print(f"[Bada3Adapter] Using generic DUMMY fallback: '{name}'")
                return m
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    def _select_esf(self, ac, alt_m, mach, deltaTemp, bada_phase, flight_evolution):
        """Override: use pyBADA's static Airplane.esf() instead of the
        instance method.

        BADA 3 OPF objects do not have an instance esf() method (that is an
        XML-only attribute of BADA 4 objects).  We call the static class method
        Airplane.esf() directly with the correct arguments for each
        flight_evolution mode.

        Returns 1.0 as a safe fallback if pyBADA raises or returns a non-finite
        value (e.g. for an unrecognised evolution string or edge-case altitude).
        """
        try:
            if flight_evolution in ("acc", "dec"):
                # acc/dec ESF branches are phase-dependent (fixed constants):
                #   acc + cl, or dec + des  => ESF = 0.3  (mostly climb, some accel)
                #   dec + cl, or acc + des  => ESF = 1.7  (mostly accel, gains KE)
                pybada_phase = "cl" if bada_phase == "cl" else "des"
                esf = Airplane.esf(flightEvolution=flight_evolution,
                                   phase=pybada_phase)
            elif flight_evolution == "constM":
                # ESF for constant-Mach climb: altitude and Mach dependent formula.
                # Above the tropopause (h > 11,000 m) ESF = 1.0.
                esf = Airplane.esf(flightEvolution="constM",
                                   h=alt_m, M=mach, deltaTemp=deltaTemp)
            elif flight_evolution == "constCAS":
                # ESF for constant-CAS climb: accounts for the rising TAS as
                # air density decreases with altitude.
                esf = Airplane.esf(flightEvolution="constCAS",
                                   h=alt_m, M=mach, deltaTemp=deltaTemp)
            else:
                # constTAS: by definition ESF = 1.0 (speed is held constant,
                # so all excess power goes to altitude gain or is zero).
                esf = 1.0

            return esf if math.isfinite(esf) else 1.0
        except Exception:
            return 1.0   # safe fallback for any unexpected pyBADA error

    # ------------------------------------------------------------------
    def create(self, n: int):
        self.model_refs = self._grow(self.model_refs, n, fill=None, dtype=object)
        self.is_dummy   = self._grow(self.is_dummy,   n, fill=True, dtype=bool)

        start = len(self.model_refs) - n
        for i in range(start, start + n):
            actype = self._actype_lookup(i).upper()

            if actype not in self._cached_models and actype not in self._failed_models:
                try:
                    m = Bada3Aircraft(badaVersion=self.BADA_VER, acName=actype,
                                      filePath=self.BADA_DIR)
                    self._cached_models[actype] = (m, False)
                except Exception as exc:
                    print(f"[Bada3Adapter] Failed to load BADA 3 model for {actype}: {exc}")
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
        # ac.MREF is the BADA 3 reference mass from the OPF file [kg]
        return float(ac.MREF) if ac is not None else 60000.0

    # ------------------------------------------------------------------
    def crossover_altitude_m(self, idx: int) -> float:
        """Return the CAS/Mach crossover altitude for this aircraft [m].

        The crossover altitude is where the aircraft's climb CAS (ac.V2["cl"])
        and climb Mach (ac.M["cl"]) yield the same True Airspeed.  Below the
        crossover the ICAO speed schedule uses constCAS; above it, constM.

        Falls back to 9,144 m (~FL300) if the OPF speed table is unavailable
        (e.g. for a generic dummy without type-specific climb speeds).
        """
        ac = self.model_refs[idx]
        try:
            Vcl2, Mcl = ac.V2["cl"], ac.M["cl"]
            return atm.crossOver(cas=Vcl2, Mach=Mcl)
        except Exception:
            return 9144.0   # ~FL300 fallback

    # ------------------------------------------------------------------
    def _deltaTemp(self, alt_m, tas_ms, temp_actual_k, p_pa=None):
        """Compute ISA temperature deviation [K] at the given pressure altitude.

        Mirrors the logic in BadaPerformanceModelMixin._atmosphere() but
        returns only deltaTemp for use in call-sites that need only that value.
        """
        if p_pa is not None and p_pa > 0:
            hp_m = (1.0 - (p_pa / 101325.0) ** 0.190263) * 44330.77
        else:
            hp_m = alt_m
        try:
            return atm.ISATemperatureDeviation(temperature=temp_actual_k,
                                               pressureAltitude=hp_m)
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    def get_envelope(self, idx, alt_m, tas_ms, mass_kg, temp_actual_k,
                     p_pa: float = None) -> FlightEnvelope:
        ac = self.model_refs[idx]
        if ac is None:
            return FlightEnvelope(0, 1e6, 0, -1, -100, 100, 2.0, 0, 0, is_dummy=True)

        try:
            if p_pa is not None and p_pa > 0:
                hp_m = (1.0 - (p_pa / 101325.0) ** 0.190263) * 44330.77
            else:
                hp_m = alt_m

            deltaTemp = atm.ISATemperatureDeviation(temperature=temp_actual_k,
                                                    pressureAltitude=hp_m)
            theta_val = atm.theta(h=hp_m, deltaTemp=deltaTemp)
            delta_val = atm.delta(h=hp_m, deltaTemp=deltaTemp)
            sigma_val = atm.sigma(theta=theta_val, delta=delta_val)

            # Maximum altitude from the BADA 3 OPF ceiling entry
            hmax = ac.flightEnvelope.maxAltitude(mass=mass_kg, deltaTemp=deltaTemp)

            # Speed envelope: convert CAS limits to TAS for FeasibilityFilter
            cas_ms = atm.tas2Cas(tas=tas_ms, delta=delta_val, sigma=sigma_val)
            config = ac.flightEnvelope.getConfig(
                phase="Cruise", h=alt_m, mass=mass_kg, v=cas_ms, deltaTemp=deltaTemp
            )
            if config is None:
                config = "CR"

            vmin_cas  = ac.flightEnvelope.VMin(h=alt_m, mass=mass_kg,
                                                config=config, deltaTemp=deltaTemp)
            vmax_cas  = ac.flightEnvelope.VMax(h=alt_m, deltaTemp=deltaTemp)
            vstall_cas = ac.flightEnvelope.VStall(mass=mass_kg, config=config)

            # Convert CAS [m/s] to TAS [m/s]
            vmin   = atm.cas2Tas(cas=vmin_cas,   delta=delta_val, sigma=sigma_val)
            vmax   = atm.cas2Tas(cas=vmax_cas,   delta=delta_val, sigma=sigma_val) \
                     if vmax_cas is not None else 1e6
            vstall = atm.cas2Tas(cas=vstall_cas, delta=delta_val, sigma=sigma_val)

        except Exception:
            hmax, vmin, vmax, vstall = -1, 0, 1e6, 0

        return FlightEnvelope(
            vmin_ms=vmin, vmax_ms=vmax, vstall_ms=vstall, hmax_m=hmax,
            vsmin_ms=-6000 * 0.00508, vsmax_ms=6000 * 0.00508, axmax_ms2=2.0,
            thrust_max_n=0.0, thrust_idle_n=0.0, is_dummy=self.is_dummy[idx],
        )

    # ------------------------------------------------------------------
    def compute(self, idx, alt_m, tas_ms, mass_kg, temp_actual_k, ax_ms2,
                bada_phase, flight_evolution, p_pa: float = None) -> EnergyTerms:
        """Compute TEM energy terms for one BADA 3 aircraft.

        All BADA 3 OPF calls use the top-level ac.Thrust() / ac.ff() /
        ac.ROCD() methods (not the flightEnvelope sub-object) and require
        positional arguments in the documented OPF order.
        """
        ac = self.model_refs[idx]
        if ac is None:
            return EnergyTerms(0, 0, 0, 0, 0, "CR", 0, 0)

        try:
            # --- Atmosphere --------------------------------------------------
            # Use pressure altitude (hp_m) when available for correct deltaTemp.
            if p_pa is not None and p_pa > 0:
                hp_m = (1.0 - (p_pa / 101325.0) ** 0.190263) * 44330.77
            else:
                hp_m = alt_m

            deltaTemp = atm.ISATemperatureDeviation(temperature=temp_actual_k,
                                                    pressureAltitude=hp_m)
            theta_val = atm.theta(h=hp_m, deltaTemp=deltaTemp)
            delta_val = atm.delta(h=hp_m, deltaTemp=deltaTemp)
            sigma_val = atm.sigma(theta=theta_val, delta=delta_val)
            M         = atm.tas2Mach(v=tas_ms, theta=theta_val)

            # Map pybada3dof phase string to pyBADA's "Climb"/"Descent"/"Cruise"
            bs_phase = {
                "cl":  "Climb",
                "des": "Descent",
            }.get(bada_phase, "Cruise")

            # --- Aerodynamic configuration -----------------------------------
            # getConfig() expects CAS [m/s], not TAS
            cas_ms = atm.tas2Cas(tas=tas_ms, delta=delta_val, sigma=sigma_val)
            config = ac.flightEnvelope.getConfig(
                phase=bs_phase, h=alt_m, mass=mass_kg, v=cas_ms, deltaTemp=deltaTemp
            )
            if config is None:
                config = "CR"

            # --- Aerodynamics: BADA 3 OPF API (uses sigma + TAS, not Mach) --
            CL = ac.flightEnvelope.CL(sigma=sigma_val, mass=mass_kg, tas=tas_ms)
            CD = ac.flightEnvelope.CD(CL=CL, config=config)
            D  = ac.flightEnvelope.D(sigma=sigma_val, tas=tas_ms, CD=CD)

            # --- Thrust: top-level ac.Thrust(), NOT ac.flightEnvelope.Thrust()
            T_max      = ac.Thrust(h=hp_m, deltaTemp=deltaTemp, rating="MCMB",
                                   v=tas_ms, config=config)
            T_idle_raw = ac.Thrust(h=hp_m, deltaTemp=deltaTemp, rating="LIDL",
                                   v=tas_ms, config=config)
            T_idle = max(float(np.nan_to_num(T_idle_raw, nan=0.0)), 0.0)
            T_max  = float(np.nan_to_num(T_max, nan=T_idle))

            if bada_phase == "cl":
                # Climb: full MCMB thrust.
                # NOTE: BADA 3 defines a reduced-climb-power coefficient C_red
                # to account for below-MTOW operations.  It is intentionally
                # NOT applied here (always full MCMB) for consistency with
                # the BADA 4 adapter and to simplify the implementation.
                T, ff_phase = T_max, "Climb"
            elif bada_phase == "des":
                # Descent: idle thrust (LIDL) already clamped to >= 0 above.
                T, ff_phase = T_idle, "Descent"
            else:
                # Cruise: drag-balanced thrust clamped to [T_idle, T_mcrz].
                # Uses the previous-tick ax_ms2 to avoid an algebraic loop.
                T_mcrz_raw = ac.Thrust(h=hp_m, deltaTemp=deltaTemp, rating="MCRZ",
                                       v=tas_ms, config=config)
                T_mcrz = float(np.nan_to_num(T_mcrz_raw, nan=T_max))
                T = float(np.clip(D + mass_kg * ax_ms2, T_idle, T_mcrz))
                ff_phase = "Cruise"

            # --- Fuel flow: top-level ac.ff(), NOT ac.flightEnvelope.ff() ---
            # ff() returns fuel flow in kg/s; clamp to minimum idle floor.
            ff = float(np.nan_to_num(
                ac.ff(h=hp_m, v=tas_ms, T=T, config=config, flightPhase=ff_phase),
                nan=0.0
            ))
            ff_min = float(np.nan_to_num(ac.ffMin(h=hp_m), nan=0.0))
            ff = max(ff, ff_min)

            # --- ESF: static Airplane.esf() (see _select_esf override above) -
            # flight_evolution is the only input that changes the ESF value.
            esf = self._select_esf(ac, hp_m, M, deltaTemp, bada_phase, flight_evolution)

            # --- ROCD: positional args (pyBADA BADA 3 signature) -------------
            rocd = ac.ROCD(T, D, tas_ms, mass_kg, esf, hp_m, deltaTemp)
            rocd = float(np.nan_to_num(rocd, nan=0.0))

            return EnergyTerms(
                thrust_n=T, drag_n=D, fuel_flow_kgps=ff, esf=esf, rocd_ms=rocd,
                config=config, thrust_max_n=T_max, thrust_idle_n=T_idle,
            )

        except Exception as exc:
            print(f"[Bada3Adapter] compute() error at alt={alt_m:.0f}m "
                  f"TAS={tas_ms:.1f}m/s phase={bada_phase} evo={flight_evolution}: {exc}")
            return EnergyTerms(0, 0, 0, 0, 0, "CR", 0, 0)
