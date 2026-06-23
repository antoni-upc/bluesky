"""
dynamic_bada.dynamic_aircraft
==============================
Per-aircraft dynamic state and integration.

``DynamicAircraft`` is the heart of the dynamic model.  One instance
is created for each BlueSky aircraft.  It owns:

  - The current dynamic state (thrust, phase counters, etc.)
  - A reference to its ``BadaInterface`` (performance model)

``step()`` performs one integration cycle and returns the quantities that
``plugin.py`` writes back into the BlueSky traffic arrays.

Fidelity modes
--------------
MODE 0  — PYBADAPERF-equivalent.  pyBADA forces, fuel flow, mass and
          phase are computed every tick so all performance columns
          (Thrust, THR%, FF, mass, phase) are logged correctly.
          VS and ax are NOT overridden: BlueSky's kinematic autopilot
          drives the trajectory — identical to the standalone PYBADAPERF
          plugin.  AT-speed waypoint patches are also active.

MODE 1  — MODE 0 performance + trajectory overrides.
          ROCD from pyBADA energy equation written to bs.traf.vs[i].
          Longitudinal acceleration from force balance written to ax[i].
          Heading still managed by BlueSky autopilot.
          Bank/pitch not tracked (set to 0).

MODE 2  — Full coupled lateral-longitudinal dynamics.
          Roll dynamics: bank angle tracks commanded phi at ≤ roll_rate_deg_s.
          Load factor: n = 1/cos(φ).
          Turn-induced drag: CL_turn = n·CL_level fed to drag polar.
          Heading: integrated from ψ̇ = g·tan(φ)/V_TAS (level-turn approx).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .bada_interface import BadaInterface, ForceResult, ROCDResult, AtmosState, delta_temp_from_actual
from .flight_dynamics import (
    longitudinal_acceleration,
    flight_path_angle,
    G0,
    wrap_angle_360,
)
from .config import DynBadaConfig


# ═══════════════════════════════════════════════════════════════════════════════
# Guidance commands (moved from autopilot_adapter.py)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class GuidanceCommand:
    """
    Autopilot / FMS commands for one aircraft at one timestep.

    All units are SI (m, m/s, rad).
    """
    target_hdg: float   # [deg] commanded heading (from LNAV or HDG SEL)
    target_alt: float   # [m]   commanded altitude (from VNAV or ALT SEL)
    target_tas: float   # [m/s] commanded TAS (from VNAV speed schedule)
    target_vs:  float   # [m/s] commanded VS  (from VNAV, > 0 = climb)
    phase:      str     # "Climb", "Cruise", "Descent"


# ═══════════════════════════════════════════════════════════════════════════════
# Step result — what plugin.py writes back into BlueSky
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class StepResult:
    """
    Output of ``DynamicAircraft.step()``.

    ``None`` fields mean: do not override the BlueSky value.
    """
    tas:       Optional[float]   # True airspeed override [m/s]
    vs:        Optional[float]   # Vertical speed override [m/s]
    ax:        Optional[float]   # Longitudinal acceleration override [m/s²]
    hdg:       Optional[float]   # Heading override [deg]
    bank_rad:  float             # Bank angle (always 0.0 in MODE 1)
    pitch_rad: float             # Pitch angle (always 0.0 in MODE 1)
    load_n:    float             # Load factor [-]
    thrust:    float             # Actual thrust [N]
    drag:      float             # Drag [N]
    lift:      float             # Lift [N]
    fuelflow:  float             # Fuel flow [kg/s]
    mass:      float             # Updated mass after fuel burn [kg]
    thr_norm:  float             # Normalised throttle [0, 1]
    T_max:     float             # Max thrust for logging [N]
    T_idle:    float             # Idle thrust for logging [N]
    vmin:      float             # Min CAS from envelope [m/s]  (pyBADA VMin)
    vmax:      float             # Max CAS from envelope [m/s]  (pyBADA VMax)
    vstall:    float             # Stall CAS from envelope [m/s]
    hmax:      float             # Ceiling [m]
    phase:     str               # Flight phase string


# ═══════════════════════════════════════════════════════════════════════════════
# Per-aircraft dynamic state
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class _DynState:
    """Internal mutable state for one aircraft."""
    T_actual:     float = 0.0   # actual thrust [N]
    fidelity:     int   = 1
    # Attitude state — bank_rad and pitch_rad must exist for all modes
    # so that plugin.py:cmd_dynbada can safely copy them across BADA switches.
    bank_rad:     float = 0.0   # actual bank angle [rad]; integrated in Mode 2
    pitch_rad:    float = 0.0   # pitch angle [rad]; reserved (always 0.0 for now)
    # Phase machine state (mirrors pybadaperf.py)
    phase:        str   = "Climb"   # current phase: "Climb"/"Cruise"/"Descent"
    toc_reached:  bool  = False     # True once aircraft first entered Cruise from Climb
    tod_reached:  bool  = False     # True once aircraft first entered Descent from Cruise
    phase_counter: float = 0.0     # accumulated time since candidate phase started [s]
    phase_cand:   str   = "None"   # candidate phase being accumulated
    # Peak ap.alt seen so far — used as the phase-detection altitude target before
    # TOC.  bs.traf.ap.alt is actwp.nextaltco (next *waypoint* altitude), which
    # during climb through closely-spaced altitude-constrained waypoints stays just
    # a few hundred feet above the current altitude, making alt_diff_ft tiny and
    # triggering false Cruise transitions every tick.  Tracking the running peak
    # gives the phase machine a stable, non-oscillating target.
    peak_ap_alt_m: float = 0.0     # highest ap.alt seen [m]


# ═══════════════════════════════════════════════════════════════════════════════
# Main per-aircraft class
# ═══════════════════════════════════════════════════════════════════════════════

class DynamicAircraft:
    """
    One-aircraft dynamic simulation object.

    Parameters
    ----------
    bada:     ``BadaInterface`` wrapping the aircraft performance model
    cfg:      Plugin configuration (shared singleton)
    fidelity: Initial fidelity mode (0 or 1)
    """

    def __init__(self,
                 bada: Optional[BadaInterface],
                 cfg: DynBadaConfig,
                 fidelity: int = 1) -> None:
        self._bada  = bada
        self._cfg   = cfg
        self._state = _DynState(fidelity=fidelity)

    @property
    def fidelity(self) -> int:
        return self._state.fidelity

    @fidelity.setter
    def fidelity(self, value: int) -> None:
        self._state.fidelity = value

    # ── Step ──────────────────────────────────────────────────────────────────

    def step(self,
             alt_m:       float,
             tas:         float,
             vs:          float,
             hdg:         float,
             mass:        float,
             ax:          float,
             atmos:       AtmosState,
             delta_temp:  float,
             guidance:    GuidanceCommand,
             dt:          float,
             phi_cmd:     float = 0.0) -> StepResult:
        """
        Compute one dynamic simulation step.

        Parameters
        ----------
        alt_m:      Current altitude [m]
        tas:        Current TAS [m/s]
        vs:         Current VS [m/s]
        hdg:        Current heading [deg]
        mass:       Current mass [kg]
        ax:         Current longitudinal acceleration [m/s²]
        atmos:      Pre-computed atmosphere state
        delta_temp: ISA temperature deviation [K]
        guidance:   AP commands for this timestep
        dt:         Time step [s]
        phi_cmd:    Commanded bank angle [rad] (used in Mode 2; default 0.0)

        Returns
        -------
        ``StepResult`` for plugin.py to write back into BlueSky arrays.
        """
        fid   = self._state.fidelity
        state = self._state

        # ── No BADA model: full fallback to kinematic ──────────────────────────
        # NOTE: fid == 0 no longer short-circuits here.  MODE 0 runs the full
        # pyBADA pipeline so that thrust / drag / fuelflow / mass / phase are
        # computed and logged (identical to PYBADAPERF).  Only the trajectory
        # overrides (vs, ax) are suppressed — see the fid-conditional below.
        if self._bada is None:
            return self._kinematic_fallback(mass)

        # ── Phase detection (time-persistent state machine) ────────────────────
        #
        # ap.alt is actwp.nextaltco — the altitude of the next waypoint only.
        # During climb through closely-spaced altitude-constrained waypoints
        # (e.g. SID/STAR profiles), ap.alt stays just a few hundred feet above
        # the current altitude, making alt_diff_ft tiny and causing false
        # Cruise transitions every few ticks → thrust spikes.
        #
        # Fix: track the running peak of ap.alt so that the phase machine
        # always sees a stable, monotonically-increasing target during climb.
        # Once TOC is reached the peak is reset so that subsequent descents
        # (where ap.alt decreases to each descent altitude constraint) are
        # detected correctly using the live ap.alt.
        raw_ap_alt_m = guidance.target_alt
        if not state.toc_reached:
            # Pre-TOC: use the maximum ap.alt seen so the climb phase is
            # stable even when VNAV flips nextaltco to the next small step.
            state.peak_ap_alt_m = max(state.peak_ap_alt_m, raw_ap_alt_m)
            ap_alt_m = state.peak_ap_alt_m
        else:
            # Post-TOC (cruise / descent): use live ap.alt for TOD detection.
            ap_alt_m = raw_ap_alt_m

        phase, toc, tod, counter, cand = self._get_phase(
            vs=vs, alt_m=alt_m, ap_alt_m=ap_alt_m,
            prev_phase=state.phase,
            toc_reached=state.toc_reached,
            tod_reached=state.tod_reached,
            prev_counter=state.phase_counter,
            prev_cand=state.phase_cand,
            dt=dt,
        )
        state.phase         = phase
        state.toc_reached   = toc
        state.tod_reached   = tod
        state.phase_counter = counter
        state.phase_cand    = cand
        guidance.phase = phase   # propagate to guidance

        # ── Sanitise TAS ──────────────────────────────────────────────────────
        tas = max(tas, self._cfg.min_tas_m_s)

        # ── pyBADA: aerodynamic forces + phase-selected thrust ─────────────────
        try:
            force: ForceResult = self._bada.forces(
                alt_m=alt_m, mass=mass, tas=tas, ax=ax,
                phase=phase, atmos=atmos, delta_temp=delta_temp)
        except Exception:
            return self._kinematic_fallback(mass)

        state.T_actual = force.thrust
        T_effective = force.thrust

        # Build a modified force with the effective thrust for ROCD
        force_eff = ForceResult(
            thrust=T_effective, drag=force.drag, lift=force.lift,
            T_max=force.T_max, T_idle=force.T_idle, T_mcrz=force.T_mcrz,
            CL=force.CL, CD=force.CD, M=force.M,
            config=force.config, HLid=force.HLid, LG=force.LG,
        )

        # ── pyBADA: ROCD ──────────────────────────────────────────────────────
        # rocd() computes the energy-equation rate [m/s] from T, D, v, mass, ESF.
        # vs (current vertical speed) is NOT an input — see bada_interface.rocd().
        try:
            rocd_res: ROCDResult = self._bada.rocd(
                alt_m=alt_m, mass=mass, tas=tas,
                phase=phase, force=force_eff, atmos=atmos, delta_temp=delta_temp)
            rocd = rocd_res.rocd   # [m/s], positive = climb
        except Exception:
            rocd = 0.0   # neutral fallback: hold current altitude

        # ── pyBADA: fuel flow + mass update ───────────────────────────────────
        try:
            ff_res = self._bada.fuelflow(
                alt_m=alt_m, tas=tas, mass=mass,
                phase=phase, force=force_eff, atmos=atmos, delta_temp=delta_temp)
            ff = ff_res.ff
        except Exception:
            ff = 0.0

        # Subtract fuel burn from current mass — matching PYBADAPERF which
        # simply does: m.mass[:] -= m.fuelflow[:] * dt (no model-based floor).
        # Using self._bada.mmin (BADA model OEW) as a floor was wrong: when
        # a MASS command sets mass below the generic fallback model's OEW
        # (e.g. 67 t aircraft mapped to 87 t J2H___), every tick would clamp
        # mass back UP to OEW, making mass appear frozen and ignoring the MASS
        # command.  The only guard needed is > 0 to avoid divide-by-zero.
        new_mass = max(mass - ff * dt, 1.0)

        # ── pyBADA: speed envelope update ─────────────────────────────────────
        try:
            env = self._bada.envelope(alt_m, mass, tas, atmos, delta_temp)
            vmin, vmax, vstall, hmax = env.vmin, env.vmax, env.vstall, env.hmax
        except Exception:
            vmin, vmax, vstall, hmax = 0.0, 1e6, 0.0, 1e6

        # ── Normalised throttle ────────────────────────────────────────────────
        dT = force.T_max - force.T_idle
        thr_norm = float(
            max(0.0, min(1.0, (T_effective - force.T_idle) / dT))
        ) if dT > 1.0 else 0.0

        # ── Derived geometry ───────────────────────────────────────────────────
        gamma = flight_path_angle(vs, tas, self._cfg.min_tas_m_s)
        W = new_mass * G0

        # ── Longitudinal acceleration from net force ───────────────────────────
        ax_dyn = longitudinal_acceleration(T_effective, force.drag, W, gamma, new_mass)

        # ── Trajectory overrides (fidelity-dependent) ────────────────────────
        hdg_override = None
        bank_out     = 0.0
        load_n_out   = 1.0
        drag_out     = force.drag
        lift_out     = force.lift

        if fid == 0:
            # MODE 0 — PYBADAPERF-equivalent: full pyBADA performance computed
            # above, but VS and ax are NOT written back to BlueSky so the
            # kinematic autopilot continues to drive the trajectory.
            vs_override = None
            ax_override = None
        else:
            # MODE 1/2: override VS (Climb/Descent) and ax from force balance.
            #
            # ALTITUDE-CLAMP GUARD (Bug fix #1)
            # ─────────────────────────────────
            # When the aircraft altitude is at or above the autopilot target
            # altitude and the phase machine still reports 'Climb' (because the
            # MCMB ROCD fed back from the previous tick kept VS high and blocked
            # the VS-based cruise condition), force VS to zero so that BlueSky's
            # altitude-hold autopilot can take effect and prevent the overshoot.
            # The phase machine will switch to Cruise at the next tick once VS
            # has been driven to a low value by the autopilot.
            alt_ft     = alt_m * 3.28084
            ap_alt_ft  = guidance.target_alt * 3.28084
            alt_at_cap = (phase == "Climb") and (alt_ft >= ap_alt_ft - 50.0)

            if phase in ("Climb", "Descent"):
                if alt_at_cap:
                    # At/above target altitude in Climb — suppress ROCD override.
                    # BlueSky autopilot manages level-off; vs_override=0.0 is
                    # needed to overwrite any residual positive ROCD in bs.traf.vs.
                    vs_override = 0.0
                else:
                    # ── Waypoint-altitude VS cap ─────────────────────────────────
                    # guidance.target_vs = bs.traf.actwp.vs[i] [m/s], signed:
                    #   > 0  climb gradient to reach next altitude constraint
                    #   < 0  descent gradient to reach next altitude constraint
                    #   = 0  no active VNAV altitude constraint (level leg)
                    #
                    # rocd [m/s] from pyBADA energy equation is the maximum
                    # physically achievable rate at the current thrust setting
                    # (MCMB for climb, idle for descent).  In dynamic mode we
                    # impose the scenario trajectory, so we cap rocd to the
                    # gradient the VNAV schedule actually requires.
                    #
                    # Climb  (rocd > 0, target_vs > 0):
                    #   vs_override = min(rocd, target_vs)   — never climb faster
                    #   than VNAV needs; aircraft can always climb slower if unable.
                    # Descent (rocd < 0, target_vs < 0):
                    #   vs_override = max(rocd, target_vs)   — never descend faster
                    #   than VNAV needs; max() keeps the less-negative value.
                    # No constraint (target_vs == 0): use raw pyBADA ROCD.
                    #
                    # Both rocd and target_vs are in [m/s] — no unit conversion needed.
                    target_vs = guidance.target_vs   # [m/s], signed
                    if target_vs > 0.0:
                        # Climb: cap at the scenario-required climb rate
                        vs_override = min(rocd, target_vs)
                    elif target_vs < 0.0:
                        # Descent: cap at the scenario-required descent rate
                        vs_override = max(rocd, target_vs)
                    else:
                        # No active waypoint altitude constraint — use BADA ROCD
                        vs_override = rocd
            else:
                vs_override = None

            # ax_override — only applied during Climb and Descent.
            # During Cruise: let BlueSky's kinematic autopilot manage
            # longitudinal speed changes (speed target from FMS).
            # Overriding ax in Cruise causes the aircraft to accelerate
            # before the Cruise->Descent phase switch fires (the speed
            # schedule shifts to descent CAS while thrust is still MCRZ,
            # giving a positive ax_dyn that produces a visible speed bump
            # before top-of-descent).
            if phase in ("Climb", "Descent"):
                ax_override = ax_dyn
            else:
                ax_override = None

        # ── MODE 2: coupled lateral-longitudinal dynamics ─────────────────────
        if fid == 2:
            ROLL_RATE_RAD_S = math.radians(
                getattr(self._cfg, 'roll_rate_deg_s', 5.0)
            )

            # ── Bank angle controller ─────────────────────────────────────────
            # IMPORTANT: ap.turnphi is BlueSky's *internal* LNAV bank angle and
            # is 0 between waypoints (only set during active turn arcs).  Using it
            # directly as phi_cmd means state.bank_rad stays at 0 all flight →
            # hdg_rate_rad = 0 → heading never changes → simulation never ends.
            #
            # Correct approach: derive phi_cmd from the heading error to the
            # commanded track (guidance.target_hdg = ap.trk[i]).  A proportional
            # controller saturated at the autopilot's default bank angle (bankdef)
            # gives the physically appropriate bank for the current turn demand.
            target_hdg    = guidance.target_hdg   # commanded track [deg]
            hdg_err_deg   = wrap_angle_180(target_hdg - hdg)   # [-180, +180]
            # Use autopilot's bank angle limit (bankdef ≈ 25°) as saturation
            phi_max_rad   = float(phi_cmd) if abs(phi_cmd) > 0.01 else math.radians(25.0)
            # Proportional gain: reach full bank at ≥ 45° heading error
            K_HDG_TO_BANK = phi_max_rad / 45.0
            phi_cmd_computed = float(
                max(-phi_max_rad, min(phi_max_rad, K_HDG_TO_BANK * hdg_err_deg))
            )

            # Rate-limited roll toward commanded bank
            delta_phi = phi_cmd_computed - state.bank_rad
            step_phi  = math.copysign(
                min(abs(delta_phi), ROLL_RATE_RAD_S * dt), delta_phi
            )
            state.bank_rad += step_phi

            # Load factor: n = 1/cos(φ); guard cos(φ) ≥ 0.1 to avoid ÷0 near 90°
            cos_phi  = max(math.cos(state.bank_rad), 0.1)
            load_n   = 1.0 / cos_phi

            # Turn-adjusted forces via pyBADA drag polar
            # CL_turn = n · CL_level → fed to fe.CD for induced-drag penalty
            # Lift logged as n·m·g (Fix #2 — level-flight CL would give only m·g)
            try:
                force_turn = self._bada.forces(
                    alt_m=alt_m, mass=mass, tas=tas, ax=ax,
                    phase=phase, atmos=atmos, delta_temp=delta_temp,
                    load_n=load_n)
                drag_out   = force_turn.drag
                lift_out   = force_turn.lift
                # Recompute ax with turn drag (higher induced drag slows aircraft)
                ax_dyn = longitudinal_acceleration(
                    T_effective, drag_out, W, gamma, new_mass)
                ax_override = ax_dyn
            except Exception:
                pass  # fallback: keep level-flight forces

            # ── Heading: leave to BlueSky autopilot ───────────────────────────
            # MODE 2's physics contribution is the turn-induced drag/load-factor
            # penalty computed above.  Heading itself is still managed by
            # BlueSky's LNAV autopilot (which runs after this preupdate hook).
            # Writing hdg_override here would conflict with the autopilot's
            # heading update and could freeze the heading at its spawn value,
            # causing the simulation to never terminate (aircraft never reaches
            # waypoints).  Return None to let the autopilot own heading.
            hdg_override = None

            bank_out   = state.bank_rad
            load_n_out = load_n

        return StepResult(
            tas       = None,        # TAS updated via ax (monkey-patched update_airspeed)
            vs        = vs_override,
            ax        = ax_override,
            hdg       = hdg_override,
            bank_rad  = bank_out,
            pitch_rad = state.pitch_rad,
            load_n    = load_n_out,
            thrust    = T_effective,
            drag      = drag_out,
            lift      = lift_out,
            fuelflow  = ff,
            mass      = new_mass,
            thr_norm  = thr_norm,
            T_max     = force.T_max,
            T_idle    = force.T_idle,
            vmin      = vmin,
            vmax      = vmax,
            vstall    = vstall,
            hmax      = hmax,
            phase     = phase,
        )

    # ── Internal helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _get_phase(vs: float, alt_m: float, ap_alt_m: float,
                   prev_phase: str,
                   toc_reached: bool, tod_reached: bool,
                   prev_counter: float, prev_cand: str,
                   dt: float):
        """Dynamic, non-monotonic phase machine with time-persistence.

        Identical algorithm to ``PyBada3._get_phase`` / ``PyBada4._get_phase``
        in *pybadaperf.py*, with an additional altitude-arrival override to
        break the Climb-phase deadlock that occurs in MODE 1/2.

        **MODE 1/2 deadlock (why altitude-arrival is needed):**
        In MODE 1/2 the plugin writes the pyBADA ROCD back to ``bs.traf.vs[i]``
        before calling ``_get_phase``.  At the end of the climb, pyBADA still
        reports a high ROCD (~600-800 ft/min) computed from MCMB thrust.  This
        high VS prevents ``cond_cruise`` (which requires |VS| ≤ 150 ft/min) from
        ever becoming True, so the phase machine stays in Climb forever.  The
        aircraft never transitions to Cruise → full MCMB thrust continues → the
        next waypoint (beginning of descent) finally causes an alt_diff < -500 ft
        transition to Descent, but by then the VS override still carries positive
        ROCD, and the aircraft overshoots the target altitude by thousands of ft.

        The fix: if the aircraft altitude has reached or exceeded the AP target
        altitude while in Climb phase, immediately force a Cruise transition
        (with the same persistence timer so the cruise-accum still fires quickly
        but can handle momentary overshoots).  This mirrors how real VNAV FMCs
        use altitude-arrival as the primary TOC criterion, using VS only as a
        secondary confirmation.

        Returns
        -------
        tuple (phase, toc_reached, tod_reached, counter, cand_phase)
        """
        # ── unit conversions ──────────────────────────────────────────────────
        alt_ft    = alt_m    * 3.28084
        ap_alt_ft = ap_alt_m * 3.28084
        vs_fpm    = vs * 60.0 * 3.28084

        # ── thresholds (identical to pybadaperf.py) ───────────────────────────
        MIN_CRUISE_ALT_FT          = 10000.0  # Cruise can only be selected above 10,000 ft
        TOC_ALT_BAND_FT            = 500.0    # Altitude capture band for TOC
        TOD_ALT_BAND_FT            = 500.0    # Altitude capture band for TOD
        CLIMB_VS_FPM               = 300.0    # VS to confirm climb/re-climb
        DESCENT_VS_FPM             = -300.0   # VS to confirm descent/re-descent
        LEVEL_VS_FPM               = 150.0    # VS threshold to consider level flight
        CRUISE_ACCUM_THRESHOLD     = 90.0     # seconds of persistence to accept Cruise
        TRANSITION_ACCUM_THRESHOLD = 10.0     # seconds of persistence for other transitions

        alt_diff_ft = ap_alt_ft - alt_ft

        # ── fallback initialisation ───────────────────────────────────────────
        if prev_phase not in ("Climb", "Cruise", "Descent"):
            if vs_fpm > CLIMB_VS_FPM:
                return "Climb", False, False, 0.0, "None"
            elif vs_fpm < DESCENT_VS_FPM:
                return "Descent", True, True, 0.0, "None"
            else:
                return "Cruise", True, False, 0.0, "None"

        # ── determine candidate phase ─────────────────────────────────────────
        cand_phase = prev_phase
        threshold  = 0.0

        if prev_phase == "Climb":
            # ALTITUDE-ARRIVAL guard (MODE 1/2 deadlock fix)
            # Only triggers when the aircraft is AT or ABOVE the target altitude
            # (alt_diff_ft <= 0), meaning it has genuinely reached TOC.
            # Using 200 ft was too eager: it fired during intermediate step-climb
            # captures where alt_diff_ft briefly passes through a small positive
            # value, causing spurious Climb->Cruise->Climb oscillations and the
            # observed thrust/fuel-flow spikes.
            alt_arrived = (alt_ft >= MIN_CRUISE_ALT_FT) and (alt_diff_ft <= 0.0)

            # VS-based level-off (identical to PYBADAPERF)
            vs_levelled = (abs(alt_diff_ft) <= TOC_ALT_BAND_FT and abs(vs_fpm) <= LEVEL_VS_FPM)

            # cond_cruise matches PYBADAPERF exactly, with the altitude-arrival
            # shortcut added for MODE 1/2 only (alt_arrived).
            cond_cruise  = alt_arrived or (alt_ft >= MIN_CRUISE_ALT_FT and vs_fpm < 50.0) or (
                alt_ft >= MIN_CRUISE_ALT_FT and vs_levelled)
            cond_descent = (vs_fpm < DESCENT_VS_FPM and alt_diff_ft < -TOD_ALT_BAND_FT)

            if cond_cruise:
                cand_phase = "Cruise"
                # Short persistence for altitude-arrival (genuine TOC reached);
                # longer 90 s persistence for VS-based level-off to avoid
                # accepting a momentary low-VS transient as a phase change.
                threshold  = 10.0 if alt_arrived else CRUISE_ACCUM_THRESHOLD
            elif cond_descent:
                cand_phase = "Descent"
                threshold  = TRANSITION_ACCUM_THRESHOLD

        elif prev_phase == "Cruise":
            cond_climb   = (alt_diff_ft >  TOC_ALT_BAND_FT and vs_fpm > CLIMB_VS_FPM)
            cond_descent = (alt_diff_ft < -TOD_ALT_BAND_FT and vs_fpm < DESCENT_VS_FPM)

            if cond_climb:
                cand_phase = "Climb"
                threshold  = TRANSITION_ACCUM_THRESHOLD
            elif cond_descent:
                cand_phase = "Descent"
                threshold  = TRANSITION_ACCUM_THRESHOLD

        elif prev_phase == "Descent":
            cond_cruise = (alt_ft >= MIN_CRUISE_ALT_FT) and (
                abs(alt_diff_ft) <= TOD_ALT_BAND_FT and abs(vs_fpm) <= LEVEL_VS_FPM
            )
            cond_climb  = (vs_fpm > CLIMB_VS_FPM and alt_diff_ft > TOC_ALT_BAND_FT)

            if cond_cruise:
                cand_phase = "Cruise"
                threshold  = CRUISE_ACCUM_THRESHOLD
            elif cond_climb:
                cand_phase = "Climb"
                threshold  = TRANSITION_ACCUM_THRESHOLD

        # ── state-transition logic ────────────────────────────────────────────
        if cand_phase == prev_phase:
            return prev_phase, toc_reached, tod_reached, 0.0, "None"
        else:
            new_counter = (prev_counter + dt) if prev_cand == cand_phase else dt

            if new_counter >= threshold:
                if cand_phase == "Cruise":
                    if prev_phase == "Climb":
                        toc_reached = True
                    elif prev_phase == "Descent":
                        tod_reached = False
                elif cand_phase == "Descent":
                    tod_reached = True
                elif cand_phase == "Climb":
                    # Do NOT reset toc_reached here.  toc_reached is a
                    # one-way flag meaning "has ever been in Cruise from
                    # Climb".  Resetting it on every Climb re-entry would
                    # re-enable the peak_ap_alt_m gate and restart the
                    # false CL→CR→CL oscillation cycle.
                    tod_reached = False

                return cand_phase, toc_reached, tod_reached, 0.0, "None"
            else:
                return prev_phase, toc_reached, tod_reached, new_counter, cand_phase

    def _kinematic_fallback(self, mass: float) -> StepResult:
        """Return a no-override result; BlueSky kinematic loop takes over."""
        return StepResult(
            tas=None, vs=None, ax=None, hdg=None,
            bank_rad=0.0, pitch_rad=0.0, load_n=1.0,
            thrust=0.0, drag=0.0, lift=0.0,
            fuelflow=0.0, mass=mass,
            thr_norm=0.0, T_max=0.0, T_idle=0.0,
            vmin=0.0, vmax=1e6, vstall=0.0, hmax=1e6,
            phase="Cruise",
        )
