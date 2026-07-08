"""
bridge.py

The single seam between BlueSky and everything else in this package.
No other module in ``pybada3dof/`` imports ``bluesky``.

Two responsibilities live here, both required to fully replace the
kinematic autopilot with the Point-Mass 3-DOF + TEM pipeline while
leaving BlueSky's own files untouched:

1. ``PyBada3DOFPerf`` implements the standard ``PerfBase`` interface
   (create/update/limits), exactly like the original plugin's
   ``PyBadaPerf`` dispatcher, so BlueSky's own bookkeeping (mass, bank,
   hmax, vmin/vmax, ...) keeps working for every other subsystem that
   reads them (ASAS, conflict detection, screen readouts).

2. It monkeypatches ``Traffic.update_airspeed`` — the method that
   currently assumes the autopilot can achieve any target tas/vs/hdg
   instantly.  BlueSky's own ``perf.limits()`` extension point clips the
   *target* but not the *transition* (it still applies a generic,
   aircraft-agnostic kinematic ramp afterwards) — see the architecture
   notes for why this monkeypatch, not ``limits()``, is where the real
   physics has to live.  ``update_groundspeed()`` and ``update_pos()``
   are left completely untouched: they already do the right thing once
   ``tas``/``hdg``/``vs``/``swaltsel`` are physically consistent.

Why pre-kinematic snapshots?
   The monkeypatch captures ``traf.tas``, ``traf.vs``, ``traf.alt``, and
   ``traf.ax`` BEFORE the original ``update_airspeed`` runs.  These
   pre-kinematic values are used as the initial condition for the
   force-balance integrator.  If the kinematically advanced TAS were fed
   into the BADA energy computation instead, the speed step would be
   double-counted (once from the kinematic correction, once from the
   force balance), producing an erroneously large acceleration signal
   and causing T_idle clamping during cruise.

Why ``vs_phys``?
   BlueSky's altitude-capture ramp inside ``update_airspeed`` can
   temporarily set ``traf.vs`` to a large geometric rate to correct an
   altitude deviation.  If ``traf.vs`` were used as the VS initial
   condition for the physics integrator, that geometric rate would
   pollute the BADA ROCD computation.  ``vs_phys[i]`` stores the last
   BADA-computed VS and is used instead, keeping the physics insulated
   from the kinematic override.
"""

import numpy as np
import bluesky as bs
from bluesky.traffic.performance.perfbase import PerfBase
from bluesky.tools.aero import vtas2cas, vtas2mach

from .energy.factory import PerformanceModelFactory
from .guidance_layer import GuidanceLayer
from .guidance.reference_generator import SPEED_SCHEDULE as _orig_speed_schedule
from .state import AircraftState, BlueSkyTargets, FlightMode

# Reference to the original (unpatched) Traffic.update_airspeed, kept so a
# future PERFMODEL OFF command could restore it.
_ORIGINAL_UPDATE_AIRSPEED = None

# Mapping from FlightMode vertical phase to the integer phase code BlueSky
# expects in traf.perf.phase: 3=climb, 4=cruise, 5=descent.
_PHASE_CODE = {"cl": 3, "cruise": 4, "des": 5}


class PyBada3DOFPerf(PerfBase):
    """PerfBase-facing entry point.  See module docstring.

    This class is instantiated exactly once per BlueSky session (stored in
    ``PyBada3DOFPerf.instance``) and remains alive as long as the plugin is
    loaded.  It delegates all per-aircraft physics to the GuidanceLayer.
    """

    instance = None

    def __init__(self, default_model: str = "BADA4"):
        super().__init__()
        PyBada3DOFPerf.instance = self

        with self.settrafarrays():
            self.vstall   = np.array([])           # stall TAS [m/s] (not in base PerfBase)
            self.thr_max  = np.array([])           # MCMB thrust [N] — read by SAVEHEADER
            self.thr_idle = np.array([])           # LIDL thrust [N] — read by SAVEHEADER
            # 0 = kinematic autopilot (MODE 0, passive observer)
            # 1 = 3-DOF point-mass physics (MODE 1, trajectory override)
            self.dyn_mode = np.array([], dtype=int)
            # Last BADA-physics VS [m/s].  Insulated from BlueSky's kinematic
            # altitude-capture ramp (see module docstring).
            self.vs_phys  = np.array([])
            self.hdg      = np.array([])           # heading [deg] — read by SAVEHEADER
            self.trk      = np.array([])           # track angle [deg] — read by SAVEHEADER

        self._perf_model = PerformanceModelFactory.create(default_model, self._actype_lookup)
        # GuidanceLayer holds a callable, not a direct reference, so that a
        # PERFMODEL BADA3|BADA4 switch (which reassigns self._perf_model) is
        # immediately visible to the guidance pipeline without rebuilding it.
        self._guidance_layer = GuidanceLayer(lambda: self._perf_model)
        _patch_traffic_update_airspeed(self)

    # ------------------------------------------------------------------
    # BlueSky-facing helpers
    # ------------------------------------------------------------------
    def _actype_lookup(self, idx: int) -> str:
        """Return the ICAO type string for aircraft at index `idx`."""
        return bs.traf.type[idx]

    @property
    def active_model_name(self) -> str:
        """Human-readable name of the currently active BADA adapter class."""
        return type(self._perf_model).__name__

    @property
    def BADA_VER(self):
        return self._perf_model.BADA_VER

    @property
    def BADA_DIR(self):
        return self._perf_model.BADA_DIR

    def set_model(self, name: str):
        """Called by the PERFMODEL stack command.

        Reassigns ``self._perf_model`` to a new adapter.  Because the
        GuidanceLayer was built with a provider callable
        (``lambda: self._perf_model``), this single reassignment is enough
        to switch the entire physics pipeline — no rebuild needed.
        Any existing aircraft will continue to use their already-loaded
        pyBADA model objects, which are preserved in the adapter's cache.
        """
        new_model = PerformanceModelFactory.create(name, self._actype_lookup)
        if bs.traf.ntraf > 0:
            new_model.create(bs.traf.ntraf)
        self._perf_model = new_model

    # ------------------------------------------------------------------
    # PerfBase interface
    # ------------------------------------------------------------------
    def create(self, n: int = 1):
        """Allocate per-aircraft state for `n` newly created aircraft.

        super().create(n) zero-initialises all traf-arrays, so dyn_mode must
        be explicitly set to 1 (3-DOF physics) after the parent call — the
        default of 0 from zero-initialisation is incorrect for new aircraft.
        """
        super().create(n)
        self._perf_model.create(n)
        # New aircraft default to DYNMODE 1 (3-DOF physics)
        self.dyn_mode[-n:] = 1
        start = bs.traf.ntraf - n
        for i in range(start, bs.traf.ntraf):
            # Seed mass from the BADA MREF so fuel depletion starts at a
            # physically realistic operating weight.
            self.mass[i]    = self._perf_model.initial_mass_kg(i)
            # Seed vs_phys from the kinematic VS so the first BADA physics
            # call starts from a consistent initial condition.
            self.vs_phys[i] = bs.traf.vs[i]
            self.hdg[i]     = bs.traf.hdg[i]
            self.trk[i]     = (bs.traf.trk[i]
                               if hasattr(bs.traf, 'trk') and i < len(bs.traf.trk)
                               else bs.traf.hdg[i])
            # Reset the intent-classifier hysteresis so the aircraft starts in Climb.
            self._guidance_layer.reset(i)
            if not self._perf_model.has_model(i):
                print(f"[PyBada3DOFPerf] {bs.traf.id[i]} ({bs.traf.type[i]}): "
                      f"no exact BADA match, using DUMMY fallback data.")

    def update(self, dt):
        """Refresh BADA-derived arrays and (for MODE 0) passive performance.

        Called every tick for ALL aircraft, regardless of dyn_mode:
          - get_envelope() refreshes vmin/vmax/vstall/hmax/vsmin/vsmax/axmax
            (used by BlueSky's ASAS and by FeasibilityFilter).
          - _update_mode0() runs the full TEM pipeline for MODE 0 aircraft
            and stores the results for SAVEHEADER logging.
          MODE 1 aircraft are handled in guided_update_airspeed() instead.
        """
        if bs.traf.ntraf == 0:
            return
        for i in range(bs.traf.ntraf):
            env = self._perf_model.get_envelope(
                i, bs.traf.alt[i], bs.traf.tas[i], self.mass[i], bs.traf.Temp[i],
                p_pa=float(bs.traf.p[i])
            )
            self.vmin[i]   = env.vmin_ms
            self.vmax[i]   = env.vmax_ms
            self.vstall[i] = env.vstall_ms
            self.hmax[i]   = env.hmax_m
            self.vsmin[i]  = env.vsmin_ms
            self.vsmax[i]  = env.vsmax_ms
            self.axmax[i]  = env.axmax_ms2

        self._update_mode0(dt)

    # ------------------------------------------------------------------
    def _update_mode0(self, dt):
        """Passive performance computation for DYNMODE 0 aircraft.

        Runs the pyBADA TEM pipeline (phase detection, thrust, drag, fuel
        flow, mass depletion) and stores the results for SAVEHEADER logging.
        No trajectory write-back is performed: BlueSky's kinematic autopilot
        continues to drive the aircraft's position, altitude, and speed.
        """
        _BADA_PHASE   = {FlightMode.CLIMB: "cl", FlightMode.DESCENT: "des"}
        _PHASE_FROM_MODE = {FlightMode.CLIMB: 3, FlightMode.DESCENT: 5}

        for i in range(bs.traf.ntraf):
            if i >= len(self.dyn_mode) or self.dyn_mode[i] != 0:
                continue  # MODE 1 is handled in guided_update_airspeed

            # Bank angle: use the autopilot's commanded bank angle if non-zero,
            # otherwise fall back to the default bank limit.
            turnphi = (bs.traf.ap.turnphi[i]
                       if bs.traf.ap.turnphi[i] > 1e-9
                       else bs.traf.ap.bankdef[i])

            # Route altitude scan: find the highest and lowest altitudes
            # remaining in the route (from the current active waypoint forward).
            try:
                rte = bs.traf.ap.route[i]
                iac = rte.iactwp
                remaining_alts = [a for a in rte.wpalt[iac:] if a >= 0]
                route_alt_m     = float(max(remaining_alts)) \
                                  if remaining_alts else float(bs.traf.aporasas.alt[i])
                route_min_alt_m = float(min(remaining_alts)) \
                                  if remaining_alts else float(bs.traf.aporasas.alt[i])
            except Exception:
                route_alt_m     = float(bs.traf.aporasas.alt[i])
                route_min_alt_m = float(bs.traf.aporasas.alt[i])

            targets = BlueSkyTargets(
                target_alt_m=bs.traf.aporasas.alt[i],
                target_tas_ms=bs.traf.aporasas.tas[i],
                target_vs_ms=bs.traf.aporasas.vs[i],
                target_hdg_deg=bs.traf.aporasas.hdg[i],
                bank_limit_deg=np.degrees(turnphi),
                actwp_spd_cas_ms=float(bs.traf.actwp.spd[i]),
                route_alt_m=route_alt_m,
                route_min_alt_m=route_min_alt_m,
            )

            state = AircraftState(
                lat_deg=bs.traf.lat[i], lon_deg=bs.traf.lon[i],
                alt_m=bs.traf.alt[i],
                tas_ms=bs.traf.tas[i], cas_ms=float(bs.traf.cas[i]),
                vs_ms=bs.traf.vs[i], hdg_deg=bs.traf.hdg[i],
                bank_deg=self.bank[i], mass_kg=self.mass[i],
                ax_ms2=bs.traf.ax[i],
            )

            # Classify intent for the phase-detection logging (identical to
            # MODE 1 so that logged phase codes are mode-agnostic).
            intent    = self._guidance_layer._intent_classifier.classify(
                i, state, targets, dt)
            bada_phase = _BADA_PHASE.get(intent.vertical_mode)

            # Select the ESF flight_evolution for MODE 0 (passive logging).
            # In MODE 0, BlueSky's kinematic autopilot controls aircraft speed —
            # our plugin has no authority over thrust.  The acc/dec ESF branches
            # (ESF=0.3 / ESF=1.7) are only meaningful in MODE 1 (guided), where
            # the guidance layer actually modulates thrust to achieve the speed
            # change.  In MODE 0, the speed classifier's acc/dec mode fires every
            # few ticks as TAS oscillates ±1–2 m/s around the target, toggling
            # ESF between 0.3 and 1.0 every second and causing VS spikes.
            # Therefore, always use the steady-state ICAO schedule ESF:
            #   above crossover altitude: constM
            #   below crossover altitude: constCAS
            #   cruise or level:          constTAS
            if bada_phase is not None:
                xover = self._perf_model.crossover_altitude_m(i)
                flight_evo = "constM" if bs.traf.alt[i] > xover else "constCAS"
            else:
                flight_evo = "constTAS"

            try:
                terms = self._perf_model.compute(
                    i, bs.traf.alt[i], bs.traf.tas[i], self.mass[i],
                    bs.traf.Temp[i], bs.traf.ax[i],
                    bada_phase=bada_phase, flight_evolution=flight_evo,
                    p_pa=float(bs.traf.p[i]),
                )
            except Exception:
                continue   # skip this aircraft if the pyBADA call fails

            self.thrust[i]   = terms.thrust_n
            self.drag[i]     = terms.drag_n
            self.fuelflow[i] = terms.fuel_flow_kgps
            # Clamp mass to 1 kg minimum to avoid zero-mass BADA errors
            self.mass[i]     = max(self.mass[i] - terms.fuel_flow_kgps * dt, 1.0)
            self.phase[i]    = _PHASE_FROM_MODE.get(intent.vertical_mode, 4)
            self.thr_max[i]  = terms.thrust_max_n
            self.thr_idle[i] = terms.thrust_idle_n
            self.hdg[i]      = bs.traf.hdg[i]
            self.trk[i]      = (bs.traf.trk[i]
                                if hasattr(bs.traf, 'trk') and i < len(bs.traf.trk)
                                else bs.traf.hdg[i])

            # Write BADA physics ROCD back to traf.vs so that SAVEHEADER logs
            # the physically meaningful vertical speed (from the TEM energy
            # balance) rather than BlueSky's kinematic autopilot VS.
            #
            # Without this write-back, traf.vs in MODE 0 comes from BlueSky's
            # VNAV, which alternates between the normal steepness formula and
            # unconstrained "catch-up" bursts (e.g. -(alt_error/t_remaining))
            # whenever the aircraft is "late" on its descent profile.  Because
            # our BADA ROCD is never used to drive the trajectory in MODE 0,
            # the aircraft drifts off the BADA profile every tick, making the
            # autopilot issue large catch-up VS commands and producing the
            # oscillating VS spikes seen in the logged CSV.
            #
            # Also store in vs_phys so the MODE 1 guidance layer always has a
            # valid physical VS as its initial condition on the next tick.
            bs.traf.vs[i]  = float(terms.rocd_ms)
            self.vs_phys[i] = float(terms.rocd_ms)

            # Normalised throttle: 0.0 = idle, 1.0 = max-continuous/max-climb.
            # Written to bs.traf.thr[i] for SAVEHEADER logging and screen readouts.
            _t_max  = self.thr_max[i]
            _t_idle = self.thr_idle[i]
            _t      = self.thrust[i]
            _range  = _t_max - _t_idle
            bs.traf.thr[i] = float(np.clip((_t - _t_idle) / _range, 0.0, 1.0)) \
                if _range > 0.0 else 0.0

    def limits(self, intent_v_tas, intent_vs, intent_h, ax):
        """Hard safety-net clip against the BADA flight envelope.

        This is a last-resort clamp, not the primary feasibility mechanism.
        Detailed feasibility enforcement (rate limits, TEM-consistent
        ROCD and acceleration) happens inside the GuidanceLayer for MODE 1
        aircraft.  For MODE 0 aircraft it prevents the kinematic autopilot
        from commanding speeds or altitudes that are completely outside the
        BADA envelope.
        """
        if bs.traf.ntraf == 0:
            return intent_v_tas, intent_vs, intent_h
        v = np.clip(intent_v_tas, self.vmin,
                    np.where(self.vmax > 0, self.vmax, intent_v_tas))
        h = np.where(self.hmax > 0, np.minimum(intent_h, self.hmax), intent_h)
        return v, intent_vs, h

    # ------------------------------------------------------------------
    def guided_update_airspeed(self, traf, pre_tas, pre_vs, pre_alt, pre_ax):
        """Physics override called AFTER ``_ORIGINAL_UPDATE_AIRSPEED`` has run.

        DYNMODE 0 aircraft are untouched here — the native kinematic autopilot
        result computed by the original ``update_airspeed()`` is kept as-is.
        DYNMODE 1 aircraft get their TAS, VS, and ``ax`` overwritten by the
        3-DOF BADA pipeline.  Heading remains under BlueSky's kinematic
        autopilot (MODE 1 only overrides the vertical/speed profile).

        Parameters
        ----------
        traf : Traffic
            BlueSky traffic object (post-kinematic state).
        pre_tas, pre_vs, pre_alt, pre_ax : np.ndarray
            Snapshots of TAS, VS, altitude, and acceleration taken
            BEFORE the kinematic autopilot ran (see module docstring for
            why pre-kinematic values must be used).
        """
        dt = bs.sim.simdt
        has_dyn1 = False

        for i in range(traf.ntraf):
            if i >= len(self.dyn_mode) or self.dyn_mode[i] != 1:
                continue   # DYNMODE 0 — kinematic result already correct

            has_dyn1 = True
            turnphi = (traf.ap.turnphi[i]
                       if traf.ap.turnphi[i] > 1e-9
                       else traf.ap.bankdef[i])

            # Route altitude scan: scan traf.ap.route[i].wpalt from the current
            # active waypoint index (iactwp) forward.  Only positive entries
            # are considered (negative = unconstrained waypoint).  This provides:
            #   route_alt_m     = highest altitude still committed in the route,
            #                     used as a step-climb guard in IntentClassifier.
            #   route_min_alt_m = lowest altitude still committed in the route,
            #                     used as a step-descent guard in IntentClassifier.
            try:
                rte = traf.ap.route[i]
                iac = rte.iactwp
                remaining_alts  = [a for a in rte.wpalt[iac:] if a >= 0]
                route_alt_m     = float(max(remaining_alts)) \
                                  if remaining_alts else float(traf.aporasas.alt[i])
                route_min_alt_m = float(min(remaining_alts)) \
                                  if remaining_alts else float(traf.aporasas.alt[i])
            except Exception:
                route_alt_m     = float(traf.aporasas.alt[i])
                route_min_alt_m = float(traf.aporasas.alt[i])

            targets = BlueSkyTargets(
                target_alt_m=traf.aporasas.alt[i],
                target_tas_ms=traf.aporasas.tas[i],
                target_vs_ms=traf.aporasas.vs[i],
                target_hdg_deg=traf.aporasas.hdg[i],
                bank_limit_deg=np.degrees(turnphi),
                # traf.actwp.spd is the current-leg speed constraint [m/s CAS],
                # set by the autopilot when it passes each waypoint.  -1.0 means
                # no speed constraint is assigned to this leg.  This is read
                # directly from actwp.spd (NOT from aporasas) because aporasas
                # tracks the aircraft's current speed when swvnavspd is off.
                actwp_spd_cas_ms=float(traf.actwp.spd[i]),
                route_alt_m=route_alt_m,
                route_min_alt_m=route_min_alt_m,
            )

            # Use pre-kinematic TAS and alt as the initial condition for the
            # force-balance integrator to avoid double-counting the kinematic
            # speed step.  For VS, use self.vs_phys[i] — the last BADA-physics
            # VS — rather than pre_vs[i], which could be corrupted by BlueSky's
            # altitude-capture ramp.  CAS is recomputed from the pre-kinematic
            # TAS and alt to stay self-consistent.
            pre_cas_i = float(vtas2cas(np.array([pre_tas[i]]),
                                       np.array([pre_alt[i]]))[0])
            state = AircraftState(
                lat_deg=traf.lat[i], lon_deg=traf.lon[i], alt_m=pre_alt[i],
                tas_ms=pre_tas[i], cas_ms=pre_cas_i,
                vs_ms=self.vs_phys[i], hdg_deg=traf.hdg[i],
                bank_deg=self.bank[i], mass_kg=self.mass[i], ax_ms2=pre_ax[i],
            )

            try:
                new_state = self._guidance_layer.step(
                    i, targets, state, traf.Temp[i], dt, p_pa=float(traf.p[i])
                )
            except Exception as exc:
                print(f"[PyBada3DOFPerf] {bs.traf.id[i]}: MODE 1 GuidanceLayer "
                      f"failed ({exc}), falling back to kinematic autopilot "
                      f"for this tick.")
                continue   # kinematic values from original update_airspeed kept

            # Write BADA physics results back to BlueSky's traf arrays.
            # Heading is also written so it stays consistent with the bank
            # angle evolution computed by BankController.
            traf.tas[i] = new_state.tas_ms
            traf.vs[i]  = new_state.vs_ms
            traf.ax[i]  = new_state.ax_ms2
            traf.hdg[i] = new_state.hdg_deg
            # Save physics VS before BlueSky can overwrite traf.vs[i] with
            # its kinematic altitude-capture ramp on the next tick.
            self.vs_phys[i] = new_state.vs_ms

            self.mass[i]      = new_state.mass_kg
            self.bank[i]      = new_state.bank_deg
            self.hdg[i]       = new_state.hdg_deg
            self.trk[i]       = (traf.trk[i]
                                 if hasattr(traf, 'trk') and i < len(traf.trk)
                                 else new_state.hdg_deg)
            self.phase[i]     = _PHASE_CODE.get(new_state.phase, 4)
            self.thrust[i]    = new_state.extra.get("thrust_n",        0.0)
            self.drag[i]      = new_state.extra.get("drag_n",          0.0)
            self.fuelflow[i]  = new_state.extra.get("fuel_flow_kgps",  0.0)
            self.thr_max[i]   = new_state.extra.get("thrust_max_n",    0.0)
            self.thr_idle[i]  = new_state.extra.get("thrust_idle_n",   0.0)

            # Normalised throttle: 0.0 = idle, 1.0 = max-continuous/max-climb.
            _t_max  = self.thr_max[i]
            _t_idle = self.thr_idle[i]
            _t      = self.thrust[i]
            _range  = _t_max - _t_idle
            traf.thr[i] = float(np.clip((_t - _t_idle) / _range, 0.0, 1.0)) \
                if _range > 0.0 else 0.0

            # Force BlueSky's altitude integrator to use our physics VS.
            # swaltsel is re-created as a fresh boolean array each tick by
            # _ORIGINAL_UPDATE_AIRSPEED, so indexing here is always safe.
            traf.swaltsel[i] = True

        if has_dyn1 and traf.ntraf > 0:
            # Recompute CAS and Mach for all aircraft after TAS changes.
            # Processing the whole fleet avoids partial-update bookkeeping errors
            # and the overhead is negligible compared to the per-aircraft BADA calls.
            traf.cas = vtas2cas(traf.tas, traf.alt)
            traf.M   = vtas2mach(traf.tas, traf.alt)


def _patch_traffic_update_airspeed(perf_instance: PyBada3DOFPerf):
    """Monkeypatch Traffic.update_airspeed to inject the 3-DOF physics pass.

    This is called once at plugin initialisation.  Subsequent re-uses of the
    same ``perf_instance`` are safe because the closure captures the instance.

    The patched function:
    1. Captures pre-kinematic snapshots of TAS, VS, alt, and ax.
    2. Runs the original kinematic autopilot for all aircraft.
    3. Overwrites DYNMODE 1 aircraft with BADA 3-DOF physics using the
       pre-kinematic snapshots as initial conditions.
    """
    global _ORIGINAL_UPDATE_AIRSPEED
    from bluesky.traffic.traffic import Traffic

    if _ORIGINAL_UPDATE_AIRSPEED is None:
        _ORIGINAL_UPDATE_AIRSPEED = Traffic.update_airspeed

    def patched_update_airspeed(self):
        # Snapshot pre-kinematic state.  These values reflect the true
        # start-of-tick aircraft state, before BlueSky's speed-error control
        # law advances TAS/VS/alt.  They are used as the initial condition
        # for the force-balance integrator in guided_update_airspeed().
        pre_tas = self.tas.copy()
        pre_vs  = self.vs.copy()
        pre_alt = self.alt.copy()
        pre_ax  = self.ax.copy()

        # Step 1: run the native kinematic autopilot for every aircraft.
        #         DYNMODE 0 aircraft are fully handled here and left untouched.
        _ORIGINAL_UPDATE_AIRSPEED(self)

        # Step 2: overwrite DYNMODE 1 aircraft with 3-DOF BADA physics,
        #         using the pre-kinematic snapshot as the initial condition.
        perf_instance.guided_update_airspeed(self, pre_tas, pre_vs, pre_alt, pre_ax)

    Traffic.update_airspeed = patched_update_airspeed
