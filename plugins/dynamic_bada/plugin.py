"""
dynamic_bada.plugin
====================
BlueSky plugin entry point for the dynamic BADA performance model.

Loading
-------
    PLUGINS LOAD dynamic_bada

Stack commands
--------------
    DYNMODE  [acid] <0|1|2>     — fidelity mode globally or per aircraft
    DYNBADA  [acid] <3|4>       — BADA generation globally or per aircraft
    DYNSTATS  acid              — show full dynamic state for an aircraft
    DYNRESET                    — re-read config.yaml (no restart needed)

Fidelity modes
--------------
  0  PYBADAPERF-equivalent — full pyBADA forces / fuelflow / mass / phase
     computed every tick; VS and ax NOT overridden (BlueSky kinematic
     autopilot drives the trajectory).  AT-speed waypoint patches active.
     Output is identical to the standalone PYBADAPERF plugin.
  1  Point-mass dynamic — MODE 0 performance + pyBADA ROCD overrides vs;
     force-balance ax overrides longitudinal acceleration.  [DEFAULT]
  2  Coupled dynamic    — Mode 1 + roll dynamics, load factor, turn-induced drag.

Architecture
------------
This class inherits ``PerfBase`` and is selected as the active performance
model via ``DynamicBada.select()``.  Each simulation tick, ``update(dt)``
is called by BlueSky's timed-function scheduler.  The update loop:

  1.  Reads atmosphere, guidance and current state from ``bs.traf``
  2.  Calls ``DynamicAircraft.step()`` for each aircraft
  3.  Writes overrides back into ``bs.traf`` (vs, ax, thr)
  4.  Updates mass, fuelflow, thrust, drag in PerfBase arrays

BlueSky's own ``update_airspeed()`` / ``update_pos()`` continue to run
normally.  In MODEs 1 and 2 they see the pre-computed vs / ax.
The monkey-patched ``dynamic_update_airspeed`` replaces the kinematic
speed-error correction for Mode 1/2 aircraft so that TAS is driven
by the pyBADA force-balance acceleration rather than the autopilot
speed-error signal.
"""
from __future__ import annotations

import os
import numpy as np
import bluesky as bs
from bluesky import stack
from bluesky.core import timed_function
from bluesky.traffic.performance.perfbase import PerfBase

from .config import cfg, DynBadaConfig
from .bada_interface import make_caches, make_interface, BadaInterface, get_properties, delta_temp_from_actual
from .dynamic_aircraft import DynamicAircraft, GuidanceCommand


# ═══════════════════════════════════════════════════════════════
# AT-speed waypoint patches (ported from pybadaperf.py)
# ═══════════════════════════════════════════════════════════════

def _convert_wpt_spd(acidx, route, wpidx):
    """Convert a waypoint speed constraint to target CAS [m/s].

    Mirrors ``convert_wpt_spd`` in pybadaperf.py exactly.
    """
    if wpidx < 0 or wpidx >= route.nwp:
        return -999.0
    wpspd = route.wpspd[wpidx]
    if wpspd <= 0.0:
        return -999.0
    wpalt = route.wpalt[wpidx]
    alt = bs.traf.alt[acidx] if wpalt < 0.0 else wpalt
    if wpspd < 2.0:            # Mach number → CAS
        from bluesky.tools.aero import mach2cas
        return mach2cas(wpspd, alt)
    return wpspd               # already CAS [m/s]


def _apply_atspeed_patches() -> None:
    """Monkey-patch Autopilot.wppassingcheck and Route.direct.

    Ensures that waypoint speed constraints are applied as AT-speed
    targets (aircraft targets the waypoint speed immediately on passing)
    rather than as advisory / look-ahead constraints.

    Identical logic to pybadaperf.py lines 46-86.
    """
    from bluesky.traffic.autopilot import Autopilot
    from bluesky.traffic.route import Route

    _orig_wppassingcheck = Autopilot.wppassingcheck

    def _new_wppassingcheck(self, qdr, dist):
        _orig_wppassingcheck(self, qdr, dist)
        for i in range(bs.traf.ntraf):
            route = self.route[i]
            if route is None or not (0 <= route.iactwp < route.nwp):
                continue
            tgt = _convert_wpt_spd(i, route, route.iactwp)
            if tgt > 0.0:
                bs.traf.actwp.spd[i]    = tgt
                bs.traf.actwp.spdcon[i] = tgt
            bs.traf.actwp.nextspd[i] = _convert_wpt_spd(i, route, route.iactwp + 1)

    Autopilot.wppassingcheck = _new_wppassingcheck

    _orig_direct = Route.direct

    def _new_direct(acidx, wpname):
        res = _orig_direct(acidx, wpname)
        acid  = bs.traf.id[acidx]
        route = Route._routes[acid]
        if route is None or not (0 <= route.iactwp < route.nwp):
            return res
        tgt = _convert_wpt_spd(acidx, route, route.iactwp)
        if tgt > 0.0:
            bs.traf.actwp.spd[acidx]    = tgt
            bs.traf.actwp.spdcon[acidx] = tgt
        bs.traf.actwp.nextspd[acidx] = _convert_wpt_spd(
            acidx, route, route.iactwp + 1)
        return res

    Route.direct = staticmethod(_new_direct)
    print("[dynamic_bada] Patched Autopilot.wppassingcheck and Route.direct "
          "for AT-speed waypoint semantics.")


# ═══════════════════════════════════════════════════════════════════════════════
# Plugin registration
# ═══════════════════════════════════════════════════════════════════════════════

def init_plugin():
    """Called by BlueSky when ``PLUGINS LOAD dynamic_bada`` is executed."""
    DynamicBada.select()
    _apply_update_airspeed_patch()
    _apply_atspeed_patches()          # AT-speed waypoint semantics (from pybadaperf)

    print("=" * 62)
    print("  PERFORMANCE MODEL : dynamic_bada (hybrid dynamic)")
    print(f"  Default BADA ver  : {cfg.default_bada_version}")
    print(f"  Default fidelity  : MODE {cfg.default_fidelity_mode}")
    print(f"  BADA4 directory   : {cfg.bada4_dir}")
    print(f"  BADA3 directory   : {cfg.bada3_dir}")
    print(f"  Update period     : {cfg.performance_dt} s")
    print("=" * 62)

    return {
        'plugin_name': 'DYNAMIC_BADA',
        'plugin_type': 'sim',
    }


def _apply_update_airspeed_patch() -> None:
    """
    Monkey-patch ``Traffic.update_airspeed`` so that MODE 1/2 aircraft
    integrate TAS from the pyBADA force-balance acceleration (bs.traf.ax)
    instead of the BlueSky kinematic speed-error correction AND so that
    the pyBADA ROCD is written to bs.traf.vs[i] AFTER aporasas.update()
    has run (which otherwise overwrites our preupdate ROCD write).

    For MODE 0 aircraft the original kinematic loop is used unchanged.

    Root-cause note (why ROCD override was silently dropped before)
    ----------------------------------------------------------------
    Execution order inside bs.traf.update():
      1. ap.update()       – computes ap.vs from VNAV geometry
      2. aporasas.update() – OVERWRITES aporasas.vs = abs(ap.vs)
      3. perf.limits()     – clips aporasas.vs
      4. update_airspeed() – uses aporasas.vs to compute target_vs
                             and then writes bs.traf.vs[i]

    DynamicBada.update() runs as a *preupdate* hook (step 0), which
    means that writing to aporasas.vs[i] in preupdate is clobbered by
    step 2 before update_airspeed() ever reads it.  The fix stores the
    ROCD in perf._pending_vs (set during preupdate) and applies it
    inside this patched update_airspeed() — which runs at step 4 —
    so the override survives.
    """
    import types
    original_update_airspeed = bs.traf.update_airspeed.__func__  # type: ignore[attr-defined]

    def dynamic_update_airspeed(traf_self) -> None:  # type: ignore[misc]
        """
        Replacement for Traffic.update_airspeed.

        Splits aircraft into two groups:
          - MODE 0 (or no DynamicBada active): standard kinematic update.
          - MODE 1/2: TAS ← TAS + ax * dt  (force-driven integration)
                      VS  ← pyBADA ROCD     (overrides kinematic AP VS).
        """
        perf = getattr(traf_self, 'perf', None)
        if not isinstance(perf, DynamicBada):
            # Fallback: standard kinematic update for non-DynamicBada sessions
            original_update_airspeed(traf_self)
            return

        # Identify which aircraft need force-driven TAS integration
        dynamic_mask = np.array([
            int(perf.fidelity[i]) >= 1
            for i in range(traf_self.ntraf)
        ], dtype=bool)

        # Save TAS for Mode 1/2 before kinematic update
        tas_before = np.copy(traf_self.tas)
        ax_dyn     = np.copy(traf_self.ax)

        # Run the standard kinematic update for ALL aircraft first
        # (handles heading, CAS, Mach, swaltsel, etc.)
        # For MODE 1/2 aircraft this also sets traf_self.vs[i] from the
        # kinematic AP — but we will overwrite it with the ROCD below.
        original_update_airspeed(traf_self)

        dt = bs.sim.simdt

        # For MODE 1/2: overwrite TAS with force-driven integration
        # AND overwrite VS with the ROCD stored during the preupdate hook.
        for i in range(traf_self.ntraf):
            if not dynamic_mask[i]:
                continue

            # ── VS override: apply pyBADA ROCD stored in preupdate ──────────
            # _pending_vs[i] is set by DynamicBada.update() with the
            # signed ROCD [m/s].  None means 'no override this tick'
            # (e.g. Cruise phase or altitude-cap guard).
            pending_vs = perf._pending_vs.get(i, None)
            if pending_vs is not None:
                traf_self.vs[i] = pending_vs
                # Also keep aporasas.vs consistent (used by update_pos
                # swaltsel logic on the *next* tick)
                traf_self.aporasas.vs[i] = abs(pending_vs)

            # ── TAS override: pyBADA force-balance integration ───────────────
            tas_new = tas_before[i] + ax_dyn[i] * dt
            # Clamp to speed envelope.
            # perf.vmin[i] / perf.vmax[i] are CAS values [m/s] returned
            # by pyBADA VMin/VMax; they must be converted to TAS at the
            # current altitude before clamping.
            vmin_cas = perf.vmin[i]
            vmax_cas = perf.vmax[i]
            if vmin_cas > 0.0 and vmax_cas > vmin_cas:
                from bluesky.tools.aero import cas2tas as _cas2tas
                alt_i = traf_self.alt[i]
                vmin_tas = _cas2tas(vmin_cas, alt_i)
                vmax_tas = _cas2tas(vmax_cas, alt_i)
                tas_new = float(np.clip(tas_new, vmin_tas, vmax_tas))
            traf_self.tas[i] = max(tas_new, 1.0)

    # Bind the patched function as a bound method
    bs.traf.update_airspeed = types.MethodType(dynamic_update_airspeed, bs.traf)
    print("[dynamic_bada] Patched Traffic.update_airspeed for force-driven TAS integration.")


# ═══════════════════════════════════════════════════════════════════════════════
# Performance model
# ═══════════════════════════════════════════════════════════════════════════════

class DynamicBada(PerfBase):
    """
    Dynamic BADA performance model for BlueSky.

    Inherits ``PerfBase`` to slot into BlueSky's replaceable performance
    model architecture.  Adds dynamic state arrays on top of the base
    class arrays (mass, thrust, drag, fuelflow, hmax, vmin, vmax, …).
    """

    def __init__(self) -> None:
        super().__init__()

        # ── Shared caches (one model object per type, per BADA generation) ─────
        self._b4_cache, self._b3_cache = make_caches(cfg)

        # ── Per-tick ROCD override storage (index → signed VS [m/s]) ──────────
        # Populated by update() (preupdate hook) and consumed by the
        # monkey-patched dynamic_update_airspeed() which runs later in
        # the same tick (inside bs.traf.update()).  Using a plain dict
        # (not a TrafficArray) keeps reset/deletion simple.
        self._pending_vs: dict = {}

        # ── Per-aircraft dynamic objects (object array) ────────────────────────
        with self.settrafarrays():
            self.dyn_ac      = np.array([], dtype=object)   # DynamicAircraft
            self.fidelity    = np.array([], dtype=int)       # 0 or 1
            self.bada_ver    = np.array([], dtype=int)       # 3 or 4
            self.phase_str   = np.array([], dtype=object)    # flight phase
            self.T_actual    = np.array([])                  # actual thrust [N]
            self.T_max_arr   = np.array([])                  # MCMB thrust [N]
            self.T_idle_arr  = np.array([])                  # LIDL thrust [N]
            self.vstall      = np.array([])                  # stall TAS [m/s]
            self.is_dummy    = np.array([], dtype=bool)      # dummy/fallback model flag

        print(f"[DynamicBada] Instantiated — default MODE {cfg.default_fidelity_mode}")

    # ── Aircraft creation ──────────────────────────────────────────────────────

    def create(self, n: int = 1) -> None:
        super().create(n)

        start = bs.traf.ntraf - n
        for i in range(start, bs.traf.ntraf):
            actype    = bs.traf.type[i].upper()
            bver      = cfg.default_bada_version
            fid       = cfg.default_fidelity_mode

            # Load BADA model (lazy, from cache)
            bada_iface: BadaInterface | None = make_interface(
                actype, bver, self._b4_cache, self._b3_cache)

            self.bada_ver[i]  = bver
            self.fidelity[i]  = fid
            self.phase_str[i] = "Cruise"
            self.phase[i]       = 4
            self.is_dummy[i]  = bada_iface.is_dummy if bada_iface is not None else True

            # Create DynamicAircraft instance
            dyn = DynamicAircraft(bada_iface, cfg, fidelity=fid)
            self.dyn_ac[i] = dyn
            print(f"[dynamic_bada] Spawned aircraft {bs.traf.id[i]} with fidelity={fid}")

            # Initialise mass from BADA reference if not already set
            if self.mass[i] <= 0.0 and bada_iface is not None:
                self.mass[i] = bada_iface.mref

            # Initialise static envelope limits
            if bada_iface is not None:
                try:
                    from .bada_interface import get_properties, delta_temp_from_actual
                    atmos = get_properties(1000.0, 0.0)
                    env = bada_iface.envelope(1000.0, self.mass[i],
                                              128.6, atmos, 0.0)
                    self.vmin[i]   = env.vmin
                    self.vmax[i]   = env.vmax
                    self.vstall[i] = env.vstall
                    self.hmax[i]   = env.hmax
                except Exception:
                    self.vmin[i]   = 0.0
                    self.vmax[i]   = 1e6
                    self.vstall[i] = 0.0
                    self.hmax[i]   = 1e6

            # Initialise T_max_arr / T_idle_arr at reference conditions so
            # saveheader never logs zero in the first simulation ticks.
            if bada_iface is not None:
                try:
                    from .bada_interface import get_properties as _gp
                    _ref_atmos = _gp(1000.0, 0.0)
                    _tmax, _tidle = bada_iface.thrust_bounds(
                        alt_m=1000.0, tas=128.6,
                        atmos=_ref_atmos, delta_temp=0.0)
                    self.T_max_arr[i]  = _tmax
                    self.T_idle_arr[i] = _tidle
                except Exception:
                    pass

            self.vsmin[i] = -6000.0 * 0.00508   # ≈ −30.5 m/s
            self.vsmax[i] =  6000.0 * 0.00508   # ≈ +30.5 m/s
            self.axmax[i] =  2.0

    # ── Per-tick update ────────────────────────────────────────────────────────

    @timed_function(name='performance', dt=cfg.performance_dt, hook='preupdate')
    def update(self, dt: float = cfg.performance_dt) -> None:
        """
        Main simulation update — called each performance tick by BlueSky.

        For each aircraft:
          1. Read current state from bs.traf
          2. Compute atmosphere
          3. Build guidance command from AP / VNAV
          4. Call DynamicAircraft.step()
          5. Write overrides back to bs.traf arrays
          6. Update PerfBase arrays (mass, thrust, drag, fuelflow)

        VS override note
        ----------------
        We must NOT write the ROCD directly to bs.traf.aporasas.vs here
        because aporasas.update() (called inside bs.traf.update() *after*
        this preupdate hook) will overwrite it with abs(ap.vs).  Instead,
        the signed ROCD is stored in self._pending_vs and applied by the
        monkey-patched dynamic_update_airspeed() which runs after
        aporasas.update() inside the same tick.
        """
        # Clear pending VS overrides from the previous tick
        self._pending_vs.clear()

        for i in range(bs.traf.ntraf):
            dyn: DynamicAircraft | None = self.dyn_ac[i]
            if dyn is None:
                self.thrust[i] = self.drag[i] = self.fuelflow[i] = 0.0
                continue

            fid = self.fidelity[i]

            # ── Read current state from BlueSky ────────────────────────────────
            alt_m = bs.traf.alt[i]
            tas   = bs.traf.tas[i]
            vs    = bs.traf.vs[i]
            hdg   = bs.traf.hdg[i]
            mass  = self.mass[i]
            ax    = bs.traf.ax[i]

            # ── Atmosphere ────────────────────────────────────────────────────
            T_actual_K  = bs.traf.Temp[i]
            delta_temp  = delta_temp_from_actual(T_actual_K, alt_m)
            atmos       = get_properties(alt_m, delta_temp)

            # ── Build guidance command from BlueSky AP ─────────────────────────
            # Phase-detection target altitude: use the MAXIMUM waypoint altitude
            # in the entire remaining route rather than bs.traf.ap.alt
            # (= actwp.nextaltco = next single waypoint only).
            #
            # During climb through closely-spaced SID/STAR altitude constraints
            # (e.g. 1521 → 3552 → 6935 → 11487 → ... → 37000 ft), ap.alt
            # equals each next waypoint altitude in turn.  The aircraft briefly
            # levels off at each constraint → VS ≈ 0 → cond_cruise fires →
            # false CL→CR transition → thrust drops from MCMB to cruise → spike.
            #
            # The maximum route altitude is the cruise altitude (37000 ft here)
            # and is stable throughout the climb, so alt_diff_ft stays large
            # (far below target) and cond_cruise never fires spuriously.
            # Post-TOD, ap.alt decreases through descent constraints, which is
            # correct for descent detection — but by then max_route_alt would
            # still be the cruise FL.  To handle descent correctly we fall back
            # to ap.alt once toc_reached is True (handled inside DynamicAircraft).
            ap_alt = bs.traf.ap.alt[i] if (
                hasattr(bs.traf.ap, 'alt') and i < len(bs.traf.ap.alt)
            ) else alt_m

            # Try to read the max remaining waypoint altitude from the route.
            try:
                route = bs.traf.ap.route[i]
                if route is not None and route.nwp > 0:
                    wpalt_arr = [a for a in route.wpalt if a > 0]
                    if wpalt_arr:
                        ap_alt = max(wpalt_arr)
            except Exception:
                pass  # fall back to ap.alt already set above

            # actwp.vs is the per-segment V/S [m/s] computed by ComputeVNAV
            # to reach the active waypoint altitude constraint exactly.
            # It is the correct VS cap for dynamic mode: it encodes the required
            # vertical gradient from current position to the next constrained alt.
            actwp_vs = float(bs.traf.actwp.vs[i]) if i < len(bs.traf.actwp.vs) else 0.0

            guidance = GuidanceCommand(
                target_hdg = bs.traf.ap.trk[i],        # commanded track
                target_alt = ap_alt,                    # max-route alt for phase detection
                target_tas = bs.traf.aporasas.tas[i],  # commanded TAS
                target_vs  = actwp_vs,                 # VNAV gradient to active waypoint altitude [m/s]
                phase      = "Cruise",                  # filled in by step()
            )

            # Read commanded bank angle for Mode 2 (radians; default 0)
            phi_cmd = 0.0
            if fid >= 2:
                # Use ap.bankdef (autopilot configured bank limit, ~25 deg) as the
                # saturation cap for DynamicAircraft's heading-error bank controller.
                # ap.turnphi was used before but is 0 between LNAV turn arcs,
                # causing bank/heading-rate to be 0 all flight (Bug #1).
                import math as _math
                _ap = bs.traf.ap._refobj if hasattr(bs.traf.ap, '_refobj') else bs.traf.ap
                phi_cmd = float(
                    getattr(_ap, 'bankdef',
                            np.full(bs.traf.ntraf, _math.radians(25.0)))[i]
                )

            try:
                result = dyn.step(
                    alt_m=alt_m, tas=tas, vs=vs, hdg=hdg,
                    mass=mass, ax=ax,
                    atmos=atmos, delta_temp=delta_temp,
                    guidance=guidance, dt=dt,
                    phi_cmd=phi_cmd,
                )
            except Exception:
                # Graceful degradation: never crash the simulation.
                # Still attempt to populate T_max_arr / T_idle_arr so
                # saveheader always sees valid (non-zero) thrust bounds.
                self.thrust[i] = self.drag[i] = self.fuelflow[i] = 0.0
                try:
                    _bada = dyn._bada
                    if _bada is not None:
                        _fallback_T_max, _fallback_T_idle = _bada.thrust_bounds(
                            alt_m=alt_m, tas=max(tas, 1.0),
                            atmos=atmos, delta_temp=delta_temp)
                        self.T_max_arr[i]  = _fallback_T_max
                        self.T_idle_arr[i] = _fallback_T_idle
                except Exception:
                    pass
                continue

            # ── Write state overrides back to BlueSky ──────────────────────────
            if result.vs is not None:
                # Clamp VS to the performance-model limit (±vsmax, default
                # ±6000 ft/min = ±30.5 m/s).  The pyBADA ROCD at MCMB thrust
                # at low altitude can exceed 8000 ft/min, which is physically
                # possible but operationally unrealistic for a commercial
                # aircraft.  BlueSky's kinematic autopilot is implicitly
                # limited by the VNAV steepness gradient; MODE 1 bypasses that
                # limit and must enforce it explicitly — mirroring how result.ax
                # is already clamped to ±axmax below.
                vs_clamped = float(
                    np.clip(result.vs, -self.vsmax[i], self.vsmax[i]))
                # Store the signed ROCD for application inside
                # dynamic_update_airspeed() — which runs AFTER
                # aporasas.update() has overwritten aporasas.vs.  Writing
                # directly to bs.traf.vs[i] here would be overwritten by
                # the kinematic VS recompute in update_airspeed().
                self._pending_vs[i] = vs_clamped
                # Also write a provisional value to bs.traf.vs so that
                # any code that reads vs between preupdate and update_airspeed
                # (e.g. _get_phase on the next tick) sees a reasonable value.
                bs.traf.vs[i] = vs_clamped

            if result.ax is not None:
                bs.traf.ax[i] = float(
                    np.clip(result.ax, -self.axmax[i], self.axmax[i]))

            if result.hdg is not None:
                bs.traf.hdg[i] = result.hdg

            # ── Update PerfBase arrays ─────────────────────────────────────────
            self.thrust[i]    = result.thrust
            self.drag[i]      = result.drag
            self.fuelflow[i]  = result.fuelflow
            self.mass[i]      = result.mass
            self.T_max_arr[i] = result.T_max
            self.T_idle_arr[i]= result.T_idle

            # Normalised throttle
            bs.traf.thr[i] = result.thr_norm

            # Dynamic state
            self.phase_str[i]   = result.phase
            # Map string phase to OpenAP integer codes for saveheader
            phase_map = {"Climb": 3, "Cruise": 4, "Descent": 5}
            self.phase[i]       = phase_map.get(result.phase, 4)

            # Envelope
            if result.vmin is not None and result.vmin > 0.0:
                self.vmin[i]   = result.vmin
            if result.vmax is not None and result.vmax < 1e5:
                self.vmax[i]   = result.vmax
            if result.vstall is not None and result.vstall > 0.0:
                self.vstall[i] = result.vstall
            if result.hmax is not None and result.hmax < 1e5:
                self.hmax[i]   = result.hmax

    # ── Performance envelope limits ────────────────────────────────────────────

    def limits(self, intent_v_tas, intent_vs, intent_h, ax):
        """Clip commanded states to pyBADA envelope.

        vmin/vmax returned by pyBADA VMin/VMax are in CAS [m/s] for BADA3,
        and must be converted to TAS before clipping intent_v_tas.
        This mirrors pybadaperf.py lines 648-656 / 1121-1129 exactly.
        """
        from bluesky.tools.aero import cas2tas

        allow_v   = np.copy(intent_v_tas)
        allow_vs  = np.copy(intent_vs)
        allow_h   = np.copy(intent_h)

        # Ceiling limit — only enforced for non-dummy models
        for i in range(bs.traf.ntraf):
            if not self.is_dummy[i]:
                if self.hmax[i] > 0.0:
                    allow_h[i] = min(allow_h[i], self.hmax[i])

        # Speed envelope — per-aircraft loop so cas2tas can use per-AC altitude
        for i in range(bs.traf.ntraf):
            if self.vmin[i] > 0.0 and self.vmax[i] > 0.0:
                alt_m = bs.traf.alt[i]
                vmin_tas = cas2tas(self.vmin[i], alt_m)
                vmax_tas = cas2tas(self.vmax[i], alt_m)
                allow_v[i] = float(np.clip(allow_v[i], vmin_tas, vmax_tas))

        return allow_v, allow_vs, allow_h

    # ── Stack commands ─────────────────────────────────────────────────────────

    @stack.command(name='DYNMODE', annotations='[txt],[txt]')
    def cmd_dynmode(self, arg1: str = None, arg2: str = None):
        """DYNMODE [acid] <0|1|2>  — set fidelity mode.

        With no acid, sets all aircraft globally.
        With acid, sets only that aircraft.
        """
        if arg1 is None:
            return True, (
                "Usage: DYNMODE [acid] <0|1|2>\n"
                "  0 = legacy kinematic\n"
                "  1 = point-mass dynamic (default)\n"
                "  2 = coupled lateral-longitudinal dynamic"
            )

        if arg2 is None:
            idx = None
            try:
                mode = int(arg1)
            except ValueError:
                return False, "DYNMODE: mode must be 0, 1, or 2"
        else:
            acid_str = arg1.upper()
            idx = bs.traf.id2idx(acid_str)
            if idx < 0:
                return False, f"Aircraft with callsign {acid_str} not found"
            try:
                mode = int(arg2)
            except ValueError:
                return False, "DYNMODE: mode must be 0, 1, or 2"

        if mode not in (0, 1, 2):
            return False, "DYNMODE: mode must be 0, 1, or 2"

        if idx is None:
            cfg.default_fidelity_mode = mode
            print(f"[dynamic_bada] GLOBAL DYNMODE changed to {mode}")
            for i in range(bs.traf.ntraf):
                self._set_fidelity(i, mode)
            return True, f"All aircraft: fidelity mode set to {mode} (and default set for new aircraft)"
        else:
            self._set_fidelity(idx, mode)
            return True, f"{bs.traf.id[idx]}: fidelity mode set to {mode}"

    @stack.command(name='DYNBADA', annotations='[txt],[txt]')
    def cmd_dynbada(self, arg1: str = None, arg2: str = None):
        """DYNBADA [acid] <3|4>  — select BADA generation.

        Changes the pyBADA backend for one or all aircraft.
        The new model is loaded immediately (from cache if already used).
        """
        if arg1 is None:
            return False, "DYNBADA: version must be 3 or 4"

        if arg2 is None:
            idx = None
            try:
                version = int(arg1)
            except ValueError:
                return False, "DYNBADA: version must be 3 or 4"
        else:
            acid_str = arg1.upper()
            idx = bs.traf.id2idx(acid_str)
            if idx < 0:
                return False, f"Aircraft with callsign {acid_str} not found"
            try:
                version = int(arg2)
            except ValueError:
                return False, "DYNBADA: version must be 3 or 4"

        if version not in (3, 4):
            return False, "DYNBADA: version must be 3 or 4"

        if idx is None:
            cfg.default_bada_version = version
            targets = range(bs.traf.ntraf)
        else:
            targets = [idx]

        for i in targets:
            actype = bs.traf.type[i].upper()
            bada_iface = make_interface(actype, version,
                                        self._b4_cache, self._b3_cache)
            self.bada_ver[i] = version
            self.is_dummy[i] = bada_iface.is_dummy if bada_iface is not None else True
            fid = int(self.fidelity[i])
            old_dyn: DynamicAircraft = self.dyn_ac[i]
            new_dyn = DynamicAircraft(bada_iface, cfg, fidelity=fid)
            # Carry over attitude and thrust state
            new_dyn._state.T_actual  = old_dyn._state.T_actual
            new_dyn._state.bank_rad  = old_dyn._state.bank_rad
            new_dyn._state.pitch_rad = old_dyn._state.pitch_rad
            # Carry over phase machine state so history is not lost on BADA switch
            new_dyn._state.phase         = old_dyn._state.phase
            new_dyn._state.toc_reached   = old_dyn._state.toc_reached
            new_dyn._state.tod_reached   = old_dyn._state.tod_reached
            new_dyn._state.phase_counter = old_dyn._state.phase_counter
            new_dyn._state.phase_cand    = old_dyn._state.phase_cand
            new_dyn._state.peak_ap_alt_m = old_dyn._state.peak_ap_alt_m
            self.dyn_ac[i] = new_dyn

        if idx is None:
            return True, f"All aircraft: switched to BADA {version}"
        return True, f"{bs.traf.id[idx]}: switched to BADA {version}"

    @stack.command(name='DYNSTATS')
    def cmd_dynstats(self, idx: acid):
        """DYNSTATS acid  — show full dynamic performance state."""
        import math
        acid_id = bs.traf.id[idx]
        fid     = int(self.fidelity[idx])
        dyn     = self.dyn_ac[idx]
        bank_deg = math.degrees(dyn._state.bank_rad) if dyn is not None else 0.0
        cos_phi  = max(math.cos(dyn._state.bank_rad), 0.1) if dyn is not None else 1.0
        load_n   = 1.0 / cos_phi if fid >= 2 else 1.0
        mode2_line = (
            f"  bank={bank_deg:.1f}°  load_n={load_n:.3f}\n" if fid >= 2 else ""
        )
        return True, (
            f"{acid_id}  mode={fid}  phase={self.phase_str[idx]}\n"
            f"  BADA={self.bada_ver[idx]}  mass={self.mass[idx]:.0f}kg\n"
            f"  T={self.thrust[idx]:.0f}N  D={self.drag[idx]:.0f}N  "
            f"T_max={self.T_max_arr[idx]:.0f}N  thr={bs.traf.thr[idx]:.2f}\n"
            f"  ff={self.fuelflow[idx]*1000.:.1f}g/s  "
            f"vs={bs.traf.vs[idx]:.2f}m/s  ax={bs.traf.ax[idx]:.3f}m/s²\n"
            + mode2_line +
            f"  vmin={self.vmin[idx]:.1f}m/s  vmax={self.vmax[idx]:.1f}m/s  "
            f"vstall={self.vstall[idx]:.1f}m/s  hmax={self.hmax[idx]/0.3048:.0f}ft"
        )

    @stack.command(name='DYNRESET')
    def cmd_dynreset(self):
        """DYNRESET  — re-read config.yaml without restarting."""
        cfg.reload()
        return True, (
            f"dynamic_bada: config reloaded — "
            f"BADA{cfg.default_bada_version} MODE{cfg.default_fidelity_mode}"
        )

    @stack.command(name='MASS')
    def cmd_mass(self, idx: acid, mass_kg: float):
        """MASS acid <mass_kg>  — override aircraft mass immediately [kg].

        Overwrites the operating mass for the specified aircraft at the
        moment the command is called.  The new mass is used from the very
        next performance tick onward (fuel burn continues from this value).

        Usage:
            MASS KLM123 68000
        """
        if mass_kg <= 0.0:
            return False, f"MASS: mass must be positive (got {mass_kg} kg)"
        if mass_kg < 500.0 or mass_kg > 1_000_000.0:
            return False, (
                f"MASS: {mass_kg} kg is outside the plausible range "
                f"[500, 1 000 000] kg — did you mean to use kg (not tonnes)?"
            )

        acid_id = bs.traf.id[idx]

        # Update the PerfBase mass array used by the performance model
        self.mass[idx] = mass_kg

        return True, (
            f"{acid_id}: mass set to {mass_kg:.1f} kg"
        )

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _set_fidelity(self, i: int, mode: int) -> None:
        """Update fidelity for aircraft at index i."""
        self.fidelity[i] = mode
        dyn: DynamicAircraft = self.dyn_ac[i]
        if dyn is not None:
            dyn.fidelity = mode
