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
  When the requested ICAO type is not in the BADA 4 data directory,
  _load_fallback() first tries to load the ``Dummy-TWIN`` model (a generic
  twin-engine narrowbody), then falls back to the first loadable model in the
  BADA_DIR sorted alphabetically.  The loaded slot is flagged is_dummy=True.
  The crossover_altitude_m() method is not available for BADA 4 because
  pyBADA's BADA 4 objects do not expose climb CAS / Mach speed tables in the
  same way as BADA 3 OPF files; the crossover computation in the ICAO speed
  schedule is handled entirely in the BADA 3 adapter.
"""

import numpy as np

from pyBADA.bada4 import Bada4Aircraft

from ..state import EnergyTerms, FlightEnvelope
from .performance_model import BadaPerformanceModelMixin, IPerformanceModel


class Bada4PerformanceAdapter(BadaPerformanceModelMixin, IPerformanceModel):

    BADA_VER = "4.2"
    BADA_DIR = "/home/paucr/bluesky/.venv/lib/python3.12/site-packages/pyBADA/aircraft/BADA4/DUMMY"

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
    def _load_fallback(self):
        """Load a generic BADA 4 Dummy model as a fallback for unknown types.

        Tries Dummy-TWIN first (preferred — best stand-in for commercial
        narrowbody), then falls back to the first loadable directory in
        BADA_DIR (sorted alphabetically).  Returns None if the directory is
        completely empty or all candidates raise exceptions.
        """
        import os
        try:
            all_dirs = sorted([
                d for d in os.listdir(self.BADA_DIR)
                if os.path.isdir(os.path.join(self.BADA_DIR, d))
            ])
        except OSError:
            return None
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
                except Exception as exc:
                    print(f"[Bada4Adapter] Failed to load BADA 4 model for {actype}: {exc}")
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

        BADA 4 XML objects do not expose the OPF-style V2["cl"] / M["cl"]
        climb speed tables, so crossover altitude cannot be computed from
        first principles here.  The ICAO speed schedule crossover is handled
        by the BADA 3 adapter.  This method is present for interface
        completeness and always returns a generic troposphere midpoint fallback.
        """
        return 9144.0   # ~FL300 — typical crossover altitude for commercial jets

    # ------------------------------------------------------------------
    def get_envelope(self, idx, alt_m, tas_ms, mass_kg, temp_actual_k,
                     p_pa: float = None) -> FlightEnvelope:
        ac = self.model_refs[idx]
        if ac is None:
            return FlightEnvelope(0, 1e6, 0, -1, -100, 100, 2.0, 0, 0, is_dummy=True)

        import pyBADA.atmosphere as _atm
        theta, delta, deltaTemp, M = self._atmosphere(alt_m, tas_ms, temp_actual_k, p_pa)
        sigma = delta / theta
        # getConfig() expects CAS [m/s], not TAS — convert
        cas_ms = _atm.tas2Cas(tas=tas_ms, delta=delta, sigma=sigma)
        try:
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
        """
        ac = self.model_refs[idx]
        if ac is None:
            return EnergyTerms(0, 0, 0, 0, 0, "CR", 0, 0)

        import pyBADA.atmosphere as _atm
        theta, delta, deltaTemp, M = self._atmosphere(alt_m, tas_ms, temp_actual_k, p_pa)
        sigma   = delta / theta
        bs_phase = {"cl": "Climb", "des": "Descent"}.get(bada_phase, "Cruise")

        # getConfig() expects CAS [m/s], not TAS — convert
        cas_ms = _atm.tas2Cas(tas=tas_ms, delta=delta, sigma=sigma)
        try:
            config = ac.flightEnvelope.getConfig(
                phase=bs_phase, h=alt_m, mass=mass_kg, v=cas_ms, deltaTemp=deltaTemp
            )
        except TypeError:
            # CAS is outside all known configuration speed boundaries (e.g. near-stall
            # on initialisation or at a high-altitude edge case).  Fall back to the
            # clean cruise configuration so the simulation continues gracefully.
            config = "CR"

        HLid, LG = ac.flightEnvelope.getAeroConfig(config=config)
        CL = ac.flightEnvelope.CL(delta=delta, mass=mass_kg, M=M)
        CD = ac.flightEnvelope.CD(HLid=HLid, LG=LG, CL=CL, M=M)
        D  = ac.flightEnvelope.D(delta=delta, M=M, CD=CD)

        T_max  = ac.flightEnvelope.Thrust(delta=delta, theta=theta, M=M,
                                           deltaTemp=deltaTemp, rating="MCMB")
        T_idle = max(ac.flightEnvelope.Thrust(delta=delta, theta=theta, M=M,
                                               deltaTemp=deltaTemp, rating="LIDL"), 0.0)

        if bada_phase == "cl":
            # Climb: full MCMB thrust (no reduced-climb-power coefficient applied)
            T, ff_rating, CT = T_max, "MCMB", None
        elif bada_phase == "des":
            # Descent: idle thrust (LIDL); CT is unused when ff_rating is set
            T, ff_rating, CT = T_idle, "LIDL", None
        else:
            # Cruise: drag-balanced thrust clamped to [T_idle, T_mcrz].
            # CT is needed by the fuel flow method when no explicit rating is given.
            T_mcrz = ac.flightEnvelope.Thrust(delta=delta, theta=theta, M=M,
                                               deltaTemp=deltaTemp, rating="MCRZ")
            T      = float(np.clip(D + mass_kg * ax_ms2, T_idle, T_mcrz))
            CT     = ac.flightEnvelope.CT(delta=delta, Thrust=T)
            ff_rating = None

        # Fuel flow: use rating-based method during climb/descent; CT-based during cruise
        if ff_rating is not None:
            ff = ac.flightEnvelope.ff(delta=delta, theta=theta, deltaTemp=deltaTemp,
                                       rating=ff_rating, M=M, config=config)
        else:
            ff = ac.flightEnvelope.ff(delta=delta, theta=theta, deltaTemp=deltaTemp,
                                       CT=CT, M=M, config=config)
        # Clamp to minimum (idle) fuel flow to avoid non-physical zero values
        ff_idle = ac.flightEnvelope.ff(delta=delta, theta=theta, deltaTemp=deltaTemp,
                                        rating="LIDL", M=M, config=config)
        ff = max(ff, ff_idle)

        # ESF: inherited _select_esf() calls ac.esf(**kwargs) (BADA 4 instance method).
        # If the result is None or NaN (e.g. unrecognised evolution string), fall back
        # to 0.3 for climb/descent (BADA TEM typical value) or 1.0 for cruise.
        esf = self._select_esf(ac, alt_m, M, deltaTemp, bada_phase, flight_evolution)
        if esf is None or (isinstance(esf, float) and np.isnan(esf)):
            esf = 0.3 if bada_phase in ("cl", "des") else 1.0

        # BADA 4 ROCD() uses keyword arguments (no reducedPower argument)
        rocd = ac.ROCD(T=T, D=D, v=tas_ms, mass=mass_kg, ESF=esf,
                       h=alt_m, deltaTemp=deltaTemp)

        return EnergyTerms(
            thrust_n=T, drag_n=D, fuel_flow_kgps=ff, esf=esf, rocd_ms=rocd,
            config=config, thrust_max_n=T_max, thrust_idle_n=T_idle,
        )
