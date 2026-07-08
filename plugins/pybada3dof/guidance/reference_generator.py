"""
guidance/reference_generator.py

Converts a (feasibility-filtered) FlightIntent into a GuidanceReference
using the Total Energy Model (TEM) of the active BADA adapter.  This is
the module that answers: "given what the aircraft is trying to do, what
rate of climb, acceleration, and bank angle can it achieve right now?"

It never computes thrust or pitch directly — those are the Controller's
job.  Instead it uses the thrust/drag numbers returned by the BADA adapter
to derive the kinematic targets (ROCD, TAS rate) via the ESF split.

------------------------------------------------------------------
ESF (Energy Share Factor) selection
------------------------------------------------------------------
The ESF determines how excess engine power (T - D) is split between
altitude gain (ROCD) and speed change (TAS rate):

    ROCD     =  (T - D) * V / (m * g0) * ESF
    TAS rate =  (T - D) * (1 - ESF) / m

The `flight_evolution` string passed to the BADA adapter selects the ESF
branch (see pyBADA's Airplane.esf, reused unmodified via IPerformanceModel):

  Scenario                           flight_evolution   ESF
  ----------------------------------------------------------------
  Level flight, changing speed       constTAS           0.0  (forced)
  Accelerating while climbing/desc.  acc                0.3  (fixed)
  Decelerating while climbing/desc.  dec                1.7  (fixed)
  Climbing/descending at const CAS   constCAS           dynamic (altitude + Mach dependent)
  Climbing/descending at const Mach  constM             dynamic (altitude + Mach dependent)
  Steady cruise (hold alt & speed)   constTAS           1.0  (by definition)

For the ICAO speed schedule (default), the constCAS/constM boundary is the
crossover altitude where a given CAS equals a target Mach number.  Below
the crossover the aircraft climbs at constant CAS; above it at constant
Mach.  Use `SPDSCHED CONSCAS` to force constant-CAS throughout.
------------------------------------------------------------------
"""

from ..energy.performance_model import IPerformanceModel
from ..state import EnergyTerms, FlightIntent, FlightMode, GuidanceReference

# Maps FlightMode vertical phase to the BADA phase string used by the adapter.
_BADA_PHASE = {FlightMode.CLIMB: "cl", FlightMode.DESCENT: "des"}

# Speed schedule: "ICAO" (default) uses constM above the crossover altitude
# and constCAS below.  "CONSCAS" forces constant-CAS throughout the entire
# flight, bypassing the crossover altitude check entirely.
# Changed at runtime via the SPDSCHED stack command: SPDSCHED ICAO | SPDSCHED CONSCAS
SPEED_SCHEDULE: str = "ICAO"


class ReferenceGenerator:

    def __init__(self, performance_model_provider):
        """
        :param performance_model_provider: callable() -> IPerformanceModel.
            A lambda is used instead of a direct reference so the generator
            always sees the *current* BADA adapter after a runtime
            PERFMODEL BADA3 <-> BADA4 switch, without being rebuilt.
        """
        self._perf_provider = performance_model_provider

    def generate(self, idx: int, intent: FlightIntent, alt_m: float, tas_ms: float,
                 mass_kg: float, temp_actual_k: float, ax_ms2: float,
                 bank_limit_deg: float, turn_sign: float,
                 route_alt_m: float = -1.0,
                 p_pa: float = None) -> GuidanceReference:
        """Compute a TEM-consistent GuidanceReference for one aircraft.

        :param idx:            Aircraft index in the performance model arrays.
        :param intent:         Feasibility-filtered FlightIntent for this tick.
        :param alt_m:          Current pressure altitude [m].
        :param tas_ms:         Current True Airspeed [m/s].
        :param mass_kg:        Current aircraft mass [kg].
        :param temp_actual_k:  Actual static air temperature [K].
        :param ax_ms2:         Previous-tick longitudinal acceleration [m/s²],
                               used for the cruise thrust balance T = D + m*ax.
        :param bank_limit_deg: Maximum allowable bank angle [deg].
        :param turn_sign:      +1.0 for a right turn, -1.0 for a left turn.
        :param route_alt_m:    Highest altitude remaining in the flight plan [m];
                               passed through but not used here (used by the
                               IntentClassifier for the step-climb guard).
        :param p_pa:           Actual static pressure [Pa] at aircraft position,
                               used for accurate pressure-altitude computation.
                               Falls back to geometric alt_m if None.
        :returns: GuidanceReference with ROCD, TAS acceleration, and bank targets.
        """
        perf: IPerformanceModel = self._perf_provider()
        # None if cruise/level (no BADA phase string needed for thrust selection)
        bada_phase = _BADA_PHASE.get(intent.vertical_mode)

        # -- Level flight while changing speed: force ESF = 0 -----------------
        # The aircraft is at (or near) its target altitude but its speed differs
        # from the target.  All excess power goes to acceleration/deceleration:
        #   ROCD    = 0   (altitude held)
        #   TAS rate = (T - D) / m   [ESF = 0 -> (1 - ESF) = 1]
        # Thrust is forced to max (MCRZ/MCMB) for acceleration or idle (LIDL)
        # for deceleration, overriding the BADA adapter's default cruise thrust.
        if bada_phase is None and intent.speed_mode in (FlightMode.ACCELERATE, FlightMode.DECELERATE):
            terms = perf.compute(idx, alt_m, tas_ms, mass_kg, temp_actual_k, ax_ms2,
                                  bada_phase=None, flight_evolution="constTAS", p_pa=p_pa)
            if intent.speed_mode == FlightMode.ACCELERATE:
                terms.thrust_n = terms.thrust_max_n
                rating = "MCRZ/max (level accel)"
            else:
                terms.thrust_n = terms.thrust_idle_n
                rating = "LIDL (level decel)"
            # ESF = 0: all excess power to speed change, ROCD stays zero
            tas_rate = (terms.thrust_n - terms.drag_n) / mass_kg
            rocd = 0.0
            esf  = 0.0

        # -- Accelerating / decelerating while climbing or descending ----------
        # The aircraft has not yet reached its target speed.  The acc/dec ESF
        # branches split excess power between altitude gain and speed change:
        #   acc (climbing or descending):  ESF = 0.3  -> 30% climb, 70% accel
        #   dec (climbing or descending):  ESF = 1.7  -> 170% climb (trades KE)
        elif bada_phase is not None and intent.speed_mode in (FlightMode.ACCELERATE, FlightMode.DECELERATE):
            evo = "acc" if intent.speed_mode == FlightMode.ACCELERATE else "dec"
            terms = perf.compute(idx, alt_m, tas_ms, mass_kg, temp_actual_k, ax_ms2,
                                  bada_phase=bada_phase, flight_evolution=evo, p_pa=p_pa)
            rocd     = terms.rocd_ms
            tas_rate = (terms.thrust_n - terms.drag_n) * (1.0 - terms.esf) / mass_kg
            esf      = terms.esf
            rating   = "MCMB" if bada_phase == "cl" else "LIDL"

        # -- Holding speed while climbing / descending (at target speed) -------
        # The aircraft is on the climb/descent speed schedule.  The ESF is
        # altitude- and Mach-dependent, computed by pyBADA's Airplane.esf():
        #   constCAS (below crossover): ESF accounts for rising TAS at const CAS
        #   constM   (above crossover): ESF accounts for falling TAS at const M
        elif bada_phase is not None:
            if SPEED_SCHEDULE == "CONSCAS":
                # Force constant-CAS throughout, bypassing the crossover check.
                evo = "constCAS"
            else:
                # ICAO schedule: switch from constCAS to constM at the crossover
                # altitude where the climb CAS equals the climb Mach number.
                xover = perf.crossover_altitude_m(idx)
                evo = "constM" if alt_m > xover else "constCAS"
            terms = perf.compute(idx, alt_m, tas_ms, mass_kg, temp_actual_k, ax_ms2,
                                  bada_phase=bada_phase, flight_evolution=evo, p_pa=p_pa)
            rocd     = terms.rocd_ms
            tas_rate = (terms.thrust_n - terms.drag_n) * (1.0 - terms.esf) / mass_kg
            esf      = terms.esf
            rating   = "MCMB" if bada_phase == "cl" else "LIDL"

        # -- Steady cruise: hold both altitude and speed -----------------------
        # constTAS -> ESF = 1 by definition -> TAS rate = (T-D)*(1-1)/m = 0.
        # ROCD is forced to 0 rather than using terms.rocd_ms because at most
        # cruise conditions T_mcrz > D, which would give a slightly positive
        # ROCD and prevent the aircraft from ever stabilising at cruise FL.
        else:
            terms = perf.compute(idx, alt_m, tas_ms, mass_kg, temp_actual_k, ax_ms2,
                                  bada_phase=None, flight_evolution="constTAS", p_pa=p_pa)
            rocd     = 0.0
            tas_rate = 0.0    # ESF = 1 -> (1 - ESF) = 0 -> no speed change
            esf      = terms.esf
            rating   = "MCRZ (bounded)"

        # -- Universal altitude capture cap (all climb/descent branches) -------
        # Prevent the aircraft from overshooting its target altitude:
        #   - During climb: cap ROCD at 0 once the target altitude is reached.
        #   - During descent: cap ROCD at 0 once the target altitude is reached.
        if bada_phase == "cl":
            if alt_m >= intent.target_alt_m:
                rocd = min(rocd, 0.0)
        elif bada_phase == "des":
            if alt_m <= intent.target_alt_m:
                rocd = max(rocd, 0.0)

        # Bank angle reference: turning is managed by BankController based on
        # the heading error; ReferenceGenerator always sets the magnitude to 0
        # (no turn commanded here — turning is handled by guidance_layer.py).
        bank_ref = 0.0

        return GuidanceReference(
            rocd_ms=rocd, tas_rate_ms2=tas_rate, bank_ref_deg=bank_ref,
            esf=esf, rating=rating,
            thrust_n=terms.thrust_n, drag_n=terms.drag_n, fuel_flow_kgps=terms.fuel_flow_kgps,
            thrust_max_n=terms.thrust_max_n, thrust_idle_n=terms.thrust_idle_n,
        )
