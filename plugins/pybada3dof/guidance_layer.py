"""
guidance_layer.py

Facade that wires the full per-aircraft guidance pipeline together for one
simulation tick.  The call sequence is:

    BlueSkyTargets
        -> IntentClassifier          (what is the aircraft trying to do?)
        -> FeasibilityFilter         (clamp intent to BADA envelope)
        -> ReferenceGenerator        (TEM/ESF: compute ROCD, acceleration, bank)
        -> PointMass3DOF             (integrate equations of motion)
        -> AircraftState             (new state written back to bs.traf)

This is the only class ``bridge.py`` talks to.  Every other module in this
package (guidance/, energy/, dynamics/) is wired here and nowhere
else, which is what keeps a future 3-DOF -> 6-DOF model swap a one-line
change (see the constructor comment below).
"""


from .dynamics.point_mass_3dof import PointMass3DOF
from .guidance.feasibility_filter import FeasibilityFilter
from .guidance.intent_classifier import IntentClassifier
from .guidance.reference_generator import ReferenceGenerator
from .state import AircraftState, BlueSkyTargets, FlightEnvelope, FlightMode, ForceCommand

# String labels written back to AircraftState.phase for SAVEHEADER logging.
_PHASE_STR = {
    FlightMode.CLIMB:   "cl",
    FlightMode.DESCENT: "des",
    FlightMode.CRUISE:  "cruise",
}


class GuidanceLayer:

    def __init__(self, performance_model_provider):
        """
        :param performance_model_provider: callable() -> IPerformanceModel.
            A lambda (rather than a direct reference) is used so the layer
            always sees the *current* adapter after a runtime PERFMODEL
            BADA3 <-> BADA4 switch, without needing to be rebuilt itself.
        """
        self._intent_classifier   = IntentClassifier()
        self._feasibility_filter  = FeasibilityFilter()
        self._reference_generator = ReferenceGenerator(performance_model_provider)
        # Only this line changes for a future 6-DOF dynamics model:
        self._dynamics            = PointMass3DOF()
        self._perf_provider       = performance_model_provider

    def reset(self, idx: int):
        """Reset per-aircraft hysteresis state for the given aircraft index.
        Called by bridge.py when a new aircraft is created so the intent
        classifier starts fresh (defaulting to the Climb phase).
        """
        self._intent_classifier.reset(idx)

    def step(self, idx: int, targets: BlueSkyTargets, state: AircraftState,
              temp_actual_k: float, dt: float,
              p_pa: float = None,
              envelope: FlightEnvelope = None) -> AircraftState:
        """Advance one aircraft by one simulation timestep `dt` [s].

        Returns a new AircraftState; never mutates `state` in place.
        Position (lat/lon) is intentionally NOT updated here — BlueSky's
        own update_pos() already integrates the great-circle track from
        the returned tas/hdg/vs, so duplicating that here would cause
        divergence.

        Parameters
        ----------
        envelope : FlightEnvelope, optional
            Pre-computed BADA flight envelope for this aircraft.  When
            provided by bridge.py (which already called get_envelope() for
            all aircraft in its ``update()`` loop), the redundant second
            call inside FeasibilityFilter is skipped entirely, halving the
            per-aircraft atmosphere computation cost for MODE 1 aircraft.
            When None (default), get_envelope() is called here as before.
        """
        perf = self._perf_provider()

        # 1. Classify operational intent (climb/cruise/descent, accel/hold/decel)
        intent = self._intent_classifier.classify(idx, state, targets, dt)

        # 2. Query current BADA flight envelope and clamp intent to it
        if envelope is None:
            envelope = perf.get_envelope(idx, state.alt_m, state.tas_ms, state.mass_kg,
                                         temp_actual_k, p_pa)
        intent = self._feasibility_filter.apply(intent, envelope)

        # 3. Compute turn direction for future lateral-guidance extensions.
        #    dhdg is in [-180, +180]: negative = left turn, positive = right turn.
        #    Currently, lateral guidance (bank angle, heading) is delegated to
        #    BlueSky's kinematic autopilot; turn_sign is passed to generate() for
        #    future TEM-derived bank-scheduling use only.
        turn_sign = 1.0
        dhdg = (targets.target_hdg_deg - state.hdg_deg + 180.0) % 360.0 - 180.0
        if dhdg < 0:
            turn_sign = -1.0

        # 4. Compute TEM-based kinematic reference (ROCD, TAS rate, bank)
        reference = self._reference_generator.generate(
            idx, intent, state.alt_m, state.tas_ms, state.mass_kg, temp_actual_k,
            state.ax_ms2, targets.bank_limit_deg, turn_sign,
            route_alt_m=targets.route_alt_m,
            p_pa=p_pa,
        )

        # 5. Assemble the force command.
        #    bank_ref_deg is 0.0 (lateral guidance delegated to BlueSky autopilot).
        command = ForceCommand(
            thrust_n=reference.thrust_n,
            drag_n=reference.drag_n,
            bank_deg=reference.bank_ref_deg,
            vs_ms=reference.rocd_ms,
            tas_rate_ms2=reference.tas_rate_ms2,
            fuel_flow_kgps=reference.fuel_flow_kgps,
            thrust_max_n=reference.thrust_max_n,
            thrust_idle_n=reference.thrust_idle_n,
        )
        new_state = self._dynamics.integrate(state, command, dt)

        # 6. Persist the resolved vertical phase string for logging
        new_state.phase = _PHASE_STR.get(intent.vertical_mode, new_state.phase)

        return new_state
