"""
guidance/intent_classifier.py

Single source of truth for "what is this aircraft currently trying to do".

Phase detection is an exact port of the validated get_phase() function from
``dynamic_bada/bada_interface.py`` (itself a copy of pybadaperf.py's
_get_phase()).  The critical difference from naive vertical-speed-based
classifiers is that phase transitions are driven by **altitude error**
(target_alt - current_alt), NOT by the aircraft's current VS.  Using VS
directly is circular: VS is always zero on initialisation, so a VS-based
classifier always returns Cruise, ROCD stays zero, and VS never changes.

Any changes to the numeric thresholds below must be mirrored in
``dynamic_bada/bada_interface.py`` and vice-versa, since both modules must
produce identical phase sequences for the same trajectory.
"""

from dataclasses import dataclass
from typing import Dict

from ..state import AircraftState, BlueSkyTargets, FlightIntent, FlightMode

# ---------------------------------------------------------------------------
# Transition thresholds (identical to bada_interface.py)
# ---------------------------------------------------------------------------

# Minimum altitude [ft] below which the Cruise phase is never declared.
# SID procedures impose intermediate altitude holds below 10,000 ft; allowing
# Cruise there would prematurely switch from MCMB to drag-balanced thrust.
MIN_CRUISE_ALT_FT          = 10_000.0

# Altitude band [ft] around the autopilot target used for Cruise entry/exit.
TOC_ALT_BAND_FT            = 500.0    # CL -> CR capture band (tight, ~RVSM half-separation)
TOD_ALT_BAND_FT            = 500.0    # DES -> CR capture band

# The CR -> CL (step-climb) exit uses a wider band than the CL -> CR entry.
# This asymmetry prevents minor altitude oscillations near the aircraft's
# performance ceiling from repeatedly bouncing it back into Climb mode.
# Only a genuine commanded step-climb (target >> current altitude) exits Cruise.
STEP_CLIMB_ALT_BAND_FT     = 2000.0   # CR -> CL exit band

# Vertical speed thresholds [ft/min] for phase classification.
CLIMB_VS_FPM               = 300.0    # minimum VS to confirm active climb
DESCENT_VS_FPM             = -300.0   # maximum VS to confirm active descent

# Maximum VS [ft/min] considered "level" for the CL->CR and DES->CR transitions.
# Rationale: during an active BADA climb at FL115-FL260, VS is typically
# 700-2677 fpm — well above this threshold.  Near the performance ceiling
# (T ≈ D), BADA physics naturally drive VS -> 0, so 50 fpm fires only once
# the aircraft has genuinely exhausted its climb margin.
LEVEL_VS_FPM               = 50.0

# Temporal persistence thresholds [s].
# Cruise transitions require a shorter window (5 s) than active-phase
# transitions (10 s) because level-off must be detected quickly to avoid
# excessive altitude overshoot.
CRUISE_ACCUM_THRESHOLD_S      = 5.0
TRANSITION_ACCUM_THRESHOLD_S  = 10.0

# Performance-ceiling accumulator: a 30 s window backs up the VS gate when
# the aircraft is genuinely stuck near its ceiling (commanded altitude above
# BADA ceiling, VS ≈ 0 but alt_diff >> TOC_ALT_BAND_FT).  This fires
# significantly later than CRUISE_ACCUM_THRESHOLD_S to avoid spurious triggers
# during normal climb deceleration.
PERFORMANCE_CEILING_ACCUM_S   = 30.0

# Speed-mode dead-bands [m/s].
# Entry: start ACCELERATE/DECELERATE when |target - current TAS| > 5 m/s (~10 kt)
# Exit:  revert to CRUISE (hold speed) when |target - current TAS| <= 0.5 m/s (~1 kt)
# The asymmetric entry/exit prevents rapid mode oscillation at the boundary.
#
# WHY 5 m/s and not 2 m/s?
# At 2 m/s the ACCEL/DECEL mode fires every 3–5 ticks during normal speed-
# schedule flight (e.g. TAS drifts ±2 kt as the aircraft decelerates along the
# CAS→Mach schedule).  This makes the reference generator toggle between the
# acc/dec ESF (0.3 or 1.7) and constM/constCAS ESF (~1.0) on alternate ticks,
# producing large VS oscillations (+500 ↔ +2200 fpm in climb; −770 ↔ −2540 fpm
# in descent).  At 5 m/s only genuinely commanded off-schedule speed changes
# (ATC restriction, level-off acceleration) trigger the acc/dec branch; normal
# schedule tracking remains in constM/constCAS for stable, smooth VS.
ACCEL_ENTRY_DEADBAND_MS = 5.0
ACCEL_EXIT_DEADBAND_MS  = 0.5

# Heading dead-band [deg]: below this, the aircraft is considered "on heading"
# and no turn is commanded.
TURN_DEADBAND_DEG = 2.0

# Unit conversion constants
_FT_PER_M   = 3.28084
_FPM_PER_MS = 60.0 * 3.28084


@dataclass
class _PhaseHysteresis:
    """Per-aircraft mutable state for the temporal persistence filter."""
    vertical_phase: str = "cl"                    # "cl" / "cruise" / "des"
    speed_mode: FlightMode = FlightMode.CRUISE    # ACCELERATE / DECELERATE / CRUISE
    counter_s:  float = 0.0                       # time accumulated for the current candidate
    candidate:  str   = "None"                    # candidate phase string


class IntentClassifier:
    """Per-aircraft flight-intent classifier with time-persistence hysteresis.

    Phase detection is driven by altitude error (target - current altitude),
    not by the aircraft's current vertical speed.  VS is used only as a
    secondary confirmation gate for the CL->CR and DES->CR transitions, where
    it serves as a proxy for "has the BADA energy balance driven VS to zero".

    State is stored per-aircraft index in `_hysteresis` and persists across
    ticks.  Call reset(idx) when an aircraft is created to ensure a clean start.
    """

    def __init__(self):
        self._hysteresis: Dict[int, _PhaseHysteresis] = {}

    def reset(self, idx: int):
        """Initialise (or re-initialise) hysteresis state for aircraft `idx`.
        Always starts in the Climb phase so a newly created aircraft
        immediately uses MCMB thrust.
        """
        self._hysteresis[idx] = _PhaseHysteresis()

    def _vertical_phase(self, idx: int, state: AircraftState,
                        targets: BlueSkyTargets, dt: float) -> str:
        """Evaluate the vertical phase state machine for one aircraft.

        Returns the (possibly unchanged) phase string "cl" / "cruise" / "des".
        """
        h = self._hysteresis.setdefault(idx, _PhaseHysteresis())

        # Unit conversions for threshold comparisons
        alt_ft      = state.alt_m * _FT_PER_M
        tgt_alt_ft  = targets.target_alt_m * _FT_PER_M
        vs_fpm      = state.vs_ms * _FPM_PER_MS
        alt_diff_ft = tgt_alt_ft - alt_ft   # positive => need to climb

        # Route-level altitude lookups:
        # route_alt_m is the highest altitude remaining in the flight plan
        # (from traf.ap.route[i].wpalt[iactwp:]).  Unlike target_alt_m, which
        # is gated by BlueSky's VNAV swvnavvs flag and can freeze at an
        # intermediate step-climb waypoint's altitude until its lat/lon is
        # passed, route_alt_m always reflects the ultimate climb goal.
        # This is the step-climb guard: if route_alt_diff_ft >> 0, the
        # aircraft has only reached an intermediate waypoint altitude and
        # must NOT transition to Cruise yet.
        if targets.route_alt_m > 0:
            route_alt_diff_ft = (targets.route_alt_m - state.alt_m) * _FT_PER_M
        else:
            route_alt_diff_ft = alt_diff_ft   # fallback: same as local target

        # route_min_alt_m is the lowest altitude remaining in the flight plan.
        # The step-descent guard uses it: if the aircraft is significantly
        # above route_min_alt_m, a level-off at an intermediate waypoint must
        # not trigger a Cruise transition because further descent is committed.
        if targets.route_min_alt_m >= 0:
            route_min_alt_diff_ft = (state.alt_m - targets.route_min_alt_m) * _FT_PER_M
        else:
            route_min_alt_diff_ft = -alt_diff_ft   # fallback

        prev_phase = h.vertical_phase

        # -- One-time initialisation fallback ---------------------------------
        # This branch fires only if hysteresis state was never set (should only
        # happen on the very first tick after a missing reset() call).
        if prev_phase not in ("cl", "cruise", "des"):
            if alt_diff_ft > TOC_ALT_BAND_FT:
                h.vertical_phase = "cl"
            elif alt_diff_ft < -TOD_ALT_BAND_FT:
                h.vertical_phase = "des"
            else:
                h.vertical_phase = "cruise"
            h.counter_s, h.candidate = 0.0, "None"
            return h.vertical_phase

        # -- Candidate phase evaluation (altitude-error driven) ---------------
        cand_phase = prev_phase
        threshold  = 0.0

        if prev_phase == "cl":
            # CL -> CR (standard path): altitude reached within ±TOC_ALT_BAND_FT
            # AND vertical speed is essentially zero.
            # The step-climb guard (route_alt_diff_ft <= TOC_ALT_BAND_FT) prevents
            # a false Cruise declaration when the aircraft has only reached an
            # intermediate step-climb waypoint altitude — the route scan confirms
            # that no higher altitude is committed ahead.
            cond_cruise = (
                alt_ft >= MIN_CRUISE_ALT_FT
                and abs(alt_diff_ft) <= TOC_ALT_BAND_FT
                and abs(vs_fpm) <= LEVEL_VS_FPM
                and route_alt_diff_ft <= TOC_ALT_BAND_FT  # step-climb guard
            )

            # CL -> CR (performance ceiling path): BADA physics have driven VS
            # to zero because T ≈ D at the aircraft's service ceiling, even
            # though the commanded altitude is still above the current altitude.
            # Without this branch, a BADA 3 Dummy whose ceiling is below the
            # scenario cruise FL would stay in Climb forever at zero VS.
            # The step-climb guard also applies here.
            cond_ceiling = (
                alt_ft >= MIN_CRUISE_ALT_FT
                and alt_diff_ft > 0
                and abs(vs_fpm) <= LEVEL_VS_FPM
            )

            # CL -> DES: target drops well below current altitude (e.g. aborted climb)
            cond_descent = alt_diff_ft < -TOD_ALT_BAND_FT

            if cond_cruise:
                cand_phase, threshold = "cruise", CRUISE_ACCUM_THRESHOLD_S
            elif cond_ceiling:
                cand_phase, threshold = "cruise", PERFORMANCE_CEILING_ACCUM_S
            elif cond_descent:
                cand_phase, threshold = "des",    TRANSITION_ACCUM_THRESHOLD_S

        elif prev_phase == "cruise":
            # CR -> CL (step-climb): target is significantly above current altitude.
            # The wider STEP_CLIMB_ALT_BAND_FT (2000 ft >> TOC_ALT_BAND_FT 500 ft)
            # ensures minor altitude oscillations near the performance ceiling do
            # not continuously re-trigger the climb phase.
            cond_climb   = alt_diff_ft > STEP_CLIMB_ALT_BAND_FT

            # CR -> DES: target drops below current altitude.
            cond_descent = alt_diff_ft < -TOD_ALT_BAND_FT

            if cond_climb:
                cand_phase, threshold = "cl",  TRANSITION_ACCUM_THRESHOLD_S
            elif cond_descent:
                cand_phase, threshold = "des", TRANSITION_ACCUM_THRESHOLD_S

        elif prev_phase == "des":
            # DES -> CR: altitude target reached AND aircraft has levelled off.
            # The VS check (|VS| <= LEVEL_VS_FPM) is restored here — unlike the
            # CL->CR transition — because during a continuous BADA LIDL descent
            # VS is always strongly negative.  Without this gate, passing within
            # ±500 ft of an intermediate descent waypoint altitude would trigger
            # a false Cruise (observed at FL321 with VS = -4208 fpm).
            # The step-descent guard (route_min_alt_diff_ft <= TOD_ALT_BAND_FT)
            # ensures that if further descent is committed in the flight plan,
            # Cruise is not declared at an intermediate level-off.
            cond_cruise = (alt_ft >= MIN_CRUISE_ALT_FT) and (
                abs(alt_diff_ft) <= TOD_ALT_BAND_FT
                and abs(vs_fpm) <= LEVEL_VS_FPM
                and route_min_alt_diff_ft <= TOD_ALT_BAND_FT  # step-descent guard
            )

            # DES -> CL: go-around / re-climb commanded
            cond_climb = alt_diff_ft > TOC_ALT_BAND_FT

            if cond_cruise:
                cand_phase, threshold = "cruise", CRUISE_ACCUM_THRESHOLD_S
            elif cond_climb:
                cand_phase, threshold = "cl",     TRANSITION_ACCUM_THRESHOLD_S

        # -- Temporal persistence filter (shared logic) -----------------------
        # A phase change is only committed once the candidate has been
        # continuously confirmed for `threshold` seconds.  Resets both
        # counter and candidate whenever the desired direction changes.
        if cand_phase == prev_phase:
            h.counter_s, h.candidate = 0.0, "None"
            return prev_phase

        if h.candidate == cand_phase:
            h.counter_s += dt
        else:
            # New candidate: start accumulating from dt (not 0, to avoid
            # off-by-one issues with very long timesteps)
            h.counter_s = dt
            h.candidate = cand_phase

        if h.counter_s >= threshold:
            h.vertical_phase       = cand_phase
            h.counter_s, h.candidate = 0.0, "None"

        return h.vertical_phase

    def classify(self, idx: int, state: AircraftState,
                 targets: BlueSkyTargets, dt: float) -> FlightIntent:
        """Produce a FlightIntent for one aircraft at the current tick.

        Combines the vertical phase state machine with a simpler speed-mode
        hysteresis (ACCEL_ENTRY vs ACCEL_EXIT dead-bands) to avoid rapid
        mode oscillation near the target speed.
        """
        vphase = self._vertical_phase(idx, state, targets, dt)
        vertical_mode = {
            "cl":     FlightMode.CLIMB,
            "cruise": FlightMode.CRUISE,
            "des":    FlightMode.DESCENT,
        }[vphase]

        # Speed mode: hysteresis between ACCELERATE/DECELERATE and CRUISE.
        # Entry dead-band (ACCEL_ENTRY_DEADBAND_MS) is wider than the exit
        # dead-band (ACCEL_EXIT_DEADBAND_MS) to prevent rapid oscillation.
        dv = targets.target_tas_ms - state.tas_ms
        h  = self._hysteresis.setdefault(idx, _PhaseHysteresis())
        current_speed_mode = h.speed_mode

        if current_speed_mode == FlightMode.CRUISE:
            # Currently holding speed — enter ACCEL/DECEL only for large errors
            if dv > ACCEL_ENTRY_DEADBAND_MS:
                speed_mode = FlightMode.ACCELERATE
            elif dv < -ACCEL_ENTRY_DEADBAND_MS:
                speed_mode = FlightMode.DECELERATE
            else:
                speed_mode = FlightMode.CRUISE
        else:
            # Currently accelerating/decelerating — exit only when nearly at target
            if abs(dv) <= ACCEL_EXIT_DEADBAND_MS:
                speed_mode = FlightMode.CRUISE
            elif dv > 0:
                speed_mode = FlightMode.ACCELERATE
            else:
                speed_mode = FlightMode.DECELERATE

        h.speed_mode = speed_mode

        # Heading error in [-180, +180]; non-zero outside TURN_DEADBAND_DEG
        dhdg   = (targets.target_hdg_deg - state.hdg_deg + 180.0) % 360.0 - 180.0
        turning = abs(dhdg) > TURN_DEADBAND_DEG

        return FlightIntent(
            vertical_mode=vertical_mode,
            speed_mode=speed_mode,
            turning=turning,
            target_alt_m=targets.target_alt_m,
            target_tas_ms=targets.target_tas_ms,
            target_hdg_deg=targets.target_hdg_deg,
        )
