"""
pybadaperf.py – Combined pyBADA performance plugin for BlueSky.

Supports both BADA 3 and BADA 4 models in a single plugin.
Select the active model at runtime with the stack command:

    PERFMODEL BADA3
    PERFMODEL BADA4

Default model on startup: BADA 4.
"""

import numpy as np
import bluesky as bs
from bluesky import stack
from bluesky.traffic.performance.perfbase import PerfBase
import pyBADA.atmosphere as atm
from pyBADA.bada3 import Bada3Aircraft
from pyBADA.bada4 import Bada4Aircraft


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def convert_wpt_spd(acidx, route, wpidx):
    """Convert waypoint speed constraint to target calibrated airspeed (CAS)."""
    if wpidx < 0 or wpidx >= route.nwp:
        return -999.0
    wpspd = route.wpspd[wpidx]
    if wpspd <= 0.0:
        return -999.0
    wpalt = route.wpalt[wpidx]
    if wpalt < 0.0:
        alt = bs.traf.alt[acidx]
    else:
        alt = wpalt
    # Check for valid Mach or CAS
    if wpspd < 2.0:
        from bluesky.tools.aero import mach2cas
        cas = mach2cas(wpspd, alt)
    else:
        cas = wpspd
    return cas

# Monkeypatch Autopilot.wppassingcheck to update speeds to AT-speed after passing checks
from bluesky.traffic.autopilot import Autopilot
original_wppassingcheck = Autopilot.wppassingcheck

def new_wppassingcheck(self, qdr, dist):
    original_wppassingcheck(self, qdr, dist)
    
    # Override speeds to implement AT-speed semantics
    for i in range(bs.traf.ntraf):
        route = self.route[i]
        if route is not None and 0 <= route.iactwp < route.nwp:
            tgt_spd = convert_wpt_spd(i, route, route.iactwp)
            if tgt_spd > 0.0:
                bs.traf.actwp.spd[i] = tgt_spd
                bs.traf.actwp.spdcon[i] = tgt_spd
            
            next_tgt_spd = convert_wpt_spd(i, route, route.iactwp + 1)
            bs.traf.actwp.nextspd[i] = next_tgt_spd

Autopilot.wppassingcheck = new_wppassingcheck

# Monkeypatch Route.direct to update speeds to AT-speed on direct waypoint activation
from bluesky.traffic.route import Route
original_direct = Route.direct

def new_direct(acidx, wpname):
    res = original_direct(acidx, wpname)
    
    acid = bs.traf.id[acidx]
    route = Route._routes[acid]
    if route is not None and 0 <= route.iactwp < route.nwp:
        tgt_spd = convert_wpt_spd(acidx, route, route.iactwp)
        if tgt_spd > 0.0:
            bs.traf.actwp.spd[acidx] = tgt_spd
            bs.traf.actwp.spdcon[acidx] = tgt_spd
        
        next_tgt_spd = convert_wpt_spd(acidx, route, route.iactwp + 1)
        bs.traf.actwp.nextspd[acidx] = next_tgt_spd
    return res

Route.direct = staticmethod(new_direct)


def init_plugin():
    # Start with BADA 4 by default
    PyBadaPerf.select()

    active = PyBadaPerf.instance
    print("=" * 60)
    print("  PERFORMANCE MODEL : pyBADA (combined plugin)")
    print(f"  Active model      : BADA {active.active_model.BADA_VER}")
    print(f"  Data directory    : {active.active_model.BADA_DIR}")
    print("="  * 60)

    return {
        'plugin_name': 'PYBADAPERF',
        'plugin_type': 'sim',
    }


# ---------------------------------------------------------------------------
# Stack command: PERFMODEL BADA3 | BADA4
# ---------------------------------------------------------------------------

@stack.command
def perfmodel(model: str):
    """PERFMODEL BADA3|BADA4 – Switch the active pyBADA performance model.

    Args:
        model: 'BADA3' or 'BADA4'
    """
    model = model.upper().strip()
    if model not in ("BADA3", "BADA4"):
        return False, f"Unknown model '{model}'. Use BADA3 or BADA4."

    perf = PyBadaPerf.instance
    if perf is None:
        return False, "PyBadaPerf has not been instantiated yet."

    if model == "BADA3":
        perf._set_model(PyBada3(perf))
    else:
        perf._set_model(PyBada4(perf))

    active = perf.active_model
    if active is None:
        return False, "Model switch failed: active_model is None after _set_model."
    print("=" * 60)
    print(f"  PERFMODEL switched : BADA {active.BADA_VER}")
    print(f"  Data directory     : {active.BADA_DIR}")
    print("=" * 60)
    return True, f"Performance model switched to BADA {active.BADA_VER}"


# ===========================================================================
# Dispatcher – thin PerfBase wrapper that delegates to the active sub-model
# ===========================================================================

class PyBadaPerf(PerfBase):
    """Dispatcher performance model.  Delegates all work to the currently
    active sub-model (PyBada3 or PyBada4).

    A class-level reference (``instance``) is kept so that the stack command
    can reach this object without going through BlueSky's internal registry.
    """

    instance = None   # class-level singleton reference

    # ------------------------------------------------------------------
    # Convenience properties – delegate to the currently active sub-model
    # ------------------------------------------------------------------
    @property
    def BADA_VER(self):
        return self.active_model.BADA_VER if self.active_model else "?"

    @property
    def BADA_DIR(self):
        return self.active_model.BADA_DIR if self.active_model else "?"

    def __init__(self):
        super().__init__()
        PyBadaPerf.instance = self

        with self.settrafarrays():
            self.thr_max  = np.array([])
            self.thr_idle = np.array([])

        # Start with BADA 4
        self.active_model = PyBada4(self)

    # ------------------------------------------------------------------
    # Model switching
    # ------------------------------------------------------------------

    def _set_model(self, new_model):
        """Replace the active sub-model and re-create all current aircraft."""
        old_model = self.active_model
        self.active_model = new_model

        # Re-create traffic entries in the new model so it has valid state
        n = bs.traf.ntraf
        if n > 0:
            new_model.create(n)

        del old_model

    # ------------------------------------------------------------------
    # PerfBase interface – all delegated to active_model
    # ------------------------------------------------------------------

    def create(self, n=1):
        super().create(n)
        if self.active_model is not None:
            self.active_model.create(n)

    def update(self, dt):
        m = self.active_model
        if m is None:
            return
        m.update(dt)
        # Copy computed arrays back into the PerfBase arrays that BlueSky reads
        if bs.traf.ntraf > 0:
            # Update aircraft mass due to fuel burn (fuelflow is in kg/s)
            m.mass[:] -= m.fuelflow[:] * dt
            self.thrust[:] = m.thrust[:]
            self.drag[:]   = m.drag[:]
            self.fuelflow[:] = m.fuelflow[:]
            self.mass[:]   = m.mass[:]
            self.hmax[:]   = m.hmax[:]
            self.vsmin[:]  = m.vsmin[:]
            self.vsmax[:]  = m.vsmax[:]
            self.axmax[:]  = m.axmax[:]
            self.thr_max[:]  = m.thr_max[:]
            self.thr_idle[:] = m.thr_idle[:]

            # Map BADA string phases to OpenAP integer codes for saveheader
            phase_map = {"Climb": 3, "Cruise": 4, "Descent": 5}
            for i in range(bs.traf.ntraf):
                sub_ph = m.phase[i] if (hasattr(m, "phase") and m.phase is not None and i < len(m.phase)) else "Cruise"
                self.phase[i] = phase_map.get(sub_ph, 4)

    def limits(self, intent_v_tas, intent_vs, intent_h, ax):
        if self.active_model is None:
            import numpy as np
            return np.copy(intent_v_tas), np.copy(intent_vs), np.copy(intent_h)
        return self.active_model.limits(intent_v_tas, intent_vs, intent_h, ax)


# ===========================================================================
# BADA 3 sub-model
# ===========================================================================

class PyBada3(PerfBase):
    """BADA 3 performance sub-model (extracted from pybadaperf3.py)."""

    BADA_DIR = "/home/paucr/bluesky/.venv/lib/python3.12/site-packages/pyBADA/aircraft/BADA3/DUMMY"
    BADA_VER = "3.15"

    def __init__(self, parent: PyBadaPerf):
        type(self)._instance = self
        # We do NOT call PerfBase.__init__ here because PerfBase manages its
        # own traffic arrays and this sub-model shares the parent's traffic.
        # Instead we initialise the caches and arrays manually.
        self._parent = parent
        self.cached_models = {}
        self.failed_models  = set()

        # Per-aircraft arrays (same length as bs.traf)
        n = bs.traf.ntraf
        self.model_refs  = np.empty(n, dtype=object)
        self.phase       = np.empty(n, dtype=object)
        self.thr_idle    = np.zeros(n)
        self.thr_max     = np.zeros(n)
        self.vmin        = np.zeros(n)
        self.vmax        = np.zeros(n)
        self.vstall      = np.zeros(n)
        # is_dummy indicates if the aircraft uses generic fallback data.
        # Ceiling limits are ignored for these models.
        self.is_dummy    = np.ones(n, dtype=bool)
        self.phase_counter = np.zeros(n)
        self.phase_cand    = np.full(n, 'None', dtype=object)

        # These are read by the dispatcher
        self.thrust   = np.zeros(n)
        self.drag     = np.zeros(n)
        self.fuelflow = np.zeros(n)
        self.mass     = parent.mass.copy() if n > 0 else np.zeros(n)
        self.hmax     = parent.hmax.copy() if n > 0 else np.zeros(n)
        self.vsmin    = parent.vsmin.copy() if n > 0 else np.zeros(n)
        self.vsmax    = parent.vsmax.copy() if n > 0 else np.zeros(n)
        self.axmax    = parent.axmax.copy() if n > 0 else np.zeros(n)

        if n > 0:
            self.phase[:] = 'Climb'

        print(f"[PyBada3] Sub-model instantiated (BADA {self.BADA_VER})")

    # ------------------------------------------------------------------
    # Helpers to grow arrays when new aircraft are created
    # ------------------------------------------------------------------

    def _grow(self, n):
        """Extend all per-aircraft arrays by n entries."""
        def _app(arr, val=0.0, dtype=None):
            extra = np.full(n, val, dtype=dtype) if dtype else np.zeros(n)
            return np.concatenate([arr, extra])

        self.model_refs  = np.concatenate([self.model_refs, np.empty(n, dtype=object)])
        self.phase       = np.concatenate([self.phase, np.full(n, 'Cruise', dtype=object)])
        self.is_dummy    = np.concatenate([self.is_dummy, np.ones(n, dtype=bool)])
        self.phase_counter = np.concatenate([self.phase_counter, np.zeros(n)])
        self.phase_cand    = np.concatenate([self.phase_cand, np.full(n, 'None', dtype=object)])
        self.thr_idle    = _app(self.thr_idle)
        self.thr_max     = _app(self.thr_max)
        self.vmin        = _app(self.vmin)
        self.vmax        = _app(self.vmax)
        self.vstall      = _app(self.vstall)
        self.thrust      = _app(self.thrust)
        self.drag        = _app(self.drag)
        self.fuelflow    = _app(self.fuelflow)
        self.mass        = _app(self.mass)
        self.hmax        = _app(self.hmax)
        self.vsmin       = _app(self.vsmin)
        self.vsmax       = _app(self.vsmax)
        self.axmax       = _app(self.axmax)

    # ------------------------------------------------------------------

    def create(self, n=1):
        self._grow(n)

        for i in range(bs.traf.ntraf - n, bs.traf.ntraf):
            actype = bs.traf.type[i].upper()
            self.phase[i]       = 'Climb'
            self.phase_counter[i] = 0.0
            self.phase_cand[i]    = 'None'

            if actype not in self.cached_models and actype not in self.failed_models:
                try:
                    model = Bada3Aircraft(
                        badaVersion=self.BADA_VER,
                        acName=actype,
                        filePath=self.BADA_DIR,
                    )
                    self.cached_models[actype] = model
                    # Check whether pyBADA resolved to the exact type or a DUMMY fallback.
                    # Bada3Aircraft stores the matched filename in .acName; if it differs
                    # from the requested type, a generic DUMMY file was used.
                    resolved_name = getattr(model, 'acName', actype).upper().strip('_')
                    is_fallback = resolved_name != actype
                    if is_fallback:
                        print(f"[PyBada3] WARNING: {actype} not found in BADA data — "
                              f"using generic DUMMY aircraft '{resolved_name}' instead. "
                              f"Speed-envelope limits will NOT be applied. "
                              f"Install licensed BADA 3 data for accurate results.")
                    else:
                        print(f"[PyBada3] Loaded BADA 3 model for {actype}")
                    self.cached_models[actype] = (model, is_fallback)
                except Exception as exc:
                    print(f"[PyBada3] Failed to load BADA 3 model for {actype}: {exc}")
                    self.failed_models.add(actype)
                    self.cached_models[actype] = (None, True)

            entry = self.cached_models.get(actype, (None, True))
            ac, is_dummy = entry if isinstance(entry, tuple) else (entry, True)
            self.model_refs[i] = ac
            self.is_dummy[i]   = is_dummy

            if ac is not None:
                if self.mass[i] <= 0.0:
                    self.mass[i] = ac.AC.MREF
                mass = self.mass[i]
                try:
                    self.hmax[i] = ac.flightEnvelope.maxAltitude(
                        mass=mass, deltaTemp=0.0
                    )
                    config_cr = ac.flightEnvelope.getConfig(
                        phase="Cruise", h=1000.0, mass=mass,
                        v=250.0 / 1.94384, deltaTemp=0.0
                    )
                    vmin_ms = ac.flightEnvelope.VMin(
                        h=1000.0, mass=mass, config=config_cr, deltaTemp=0.0
                    )
                    vmax_ms = ac.flightEnvelope.VMax(h=1000.0, deltaTemp=0.0)
                    vstall  = ac.flightEnvelope.VStall(mass=mass, config=config_cr)

                    self.vmin[i]   = vmin_ms
                    self.vmax[i]   = vmax_ms
                    self.vstall[i] = vstall
                    self.vsmin[i]  = -6000 * 0.00508
                    self.vsmax[i]  =  6000 * 0.00508
                    # BADA 3 UM §3: typical jet transport ax_max ≈ 2 m/s²
                    self.axmax[i]  = 2
                except Exception as exc:
                    print(f"[PyBada3] Envelope init failed for {actype}: {exc}")

    @staticmethod
    def _get_phase(vs, alt_m, ap_alt_m, prev_phase, prev_counter, prev_cand, dt):
        """Determines flight phase based on vertical speed and altitude.
        
        Uses time-persistence to prevent phase oscillations during altitude capture.
        """
        # -- unit conversions --
        alt_ft    = alt_m   * 3.28084
        ap_alt_ft = ap_alt_m * 3.28084
        vs_fpm    = vs * 60.0 * 3.28084

        # -- thresholds --
        MIN_CRUISE_ALT_FT     = 10000.0 # Cruise phase can only be selected above 10,000 ft
        TOC_ALT_BAND_FT       = 500.0   # Altitude capture band for TOC
        TOD_ALT_BAND_FT       = 500.0   # Altitude capture band for TOD
        CLIMB_VS_FPM          = 300.0   # VS to confirm climb/re-climb
        DESCENT_VS_FPM        = -300.0  # VS to confirm descent/re-descent
        LEVEL_VS_FPM          = 150.0   # VS threshold to consider level flight

        CRUISE_ACCUM_THRESHOLD = 90.0
        TRANSITION_ACCUM_THRESHOLD = 10.0

        alt_diff_ft = ap_alt_ft - alt_ft

        # Fallback initialization (if prev_phase is None or invalid)
        if prev_phase not in ("Climb", "Cruise", "Descent"):
            if vs_fpm > CLIMB_VS_FPM:
                return "Climb", 0.0, "None"
            elif vs_fpm < DESCENT_VS_FPM:
                return "Descent", 0.0, "None"
            else:
                return "Cruise", 0.0, "None"

        # Determine candidate phase
        cand_phase = prev_phase
        threshold = 0.0

        if prev_phase == "Climb":
            # Climb -> Cruise (TOC reached)
            cond_cruise = (alt_ft >= MIN_CRUISE_ALT_FT) and (
                (abs(alt_diff_ft) <= TOC_ALT_BAND_FT and abs(vs_fpm) <= LEVEL_VS_FPM) or vs_fpm < 50.0
            )
            # Climb -> Descent (Aborted climb / emergency descent)
            cond_descent = (vs_fpm < DESCENT_VS_FPM and alt_diff_ft < -TOD_ALT_BAND_FT)

            if cond_cruise:
                cand_phase = "Cruise"
                threshold = CRUISE_ACCUM_THRESHOLD
            elif cond_descent:
                cand_phase = "Descent"
                threshold = TRANSITION_ACCUM_THRESHOLD

        elif prev_phase == "Cruise":
            # Cruise -> Climb (Step Climb / Re-climb)
            cond_climb = (alt_diff_ft > TOC_ALT_BAND_FT and vs_fpm > CLIMB_VS_FPM)
            # Cruise -> Descent (TOD reached)
            cond_descent = (alt_diff_ft < -TOD_ALT_BAND_FT and vs_fpm < DESCENT_VS_FPM)

            if cond_climb:
                cand_phase = "Climb"
                threshold = TRANSITION_ACCUM_THRESHOLD
            elif cond_descent:
                cand_phase = "Descent"
                threshold = TRANSITION_ACCUM_THRESHOLD

        elif prev_phase == "Descent":
            # Descent -> Cruise (Level-off / intermediate hold)
            cond_cruise = (alt_ft >= MIN_CRUISE_ALT_FT) and (
                abs(alt_diff_ft) <= TOD_ALT_BAND_FT and abs(vs_fpm) <= LEVEL_VS_FPM
            )
            # Descent -> Climb (Go-around / re-climb)
            cond_climb = (vs_fpm > CLIMB_VS_FPM and alt_diff_ft > TOC_ALT_BAND_FT)

            if cond_cruise:
                cand_phase = "Cruise"
                threshold = CRUISE_ACCUM_THRESHOLD
            elif cond_climb:
                cand_phase = "Climb"
                threshold = TRANSITION_ACCUM_THRESHOLD

        # State transition logic
        # State transition logic
        if cand_phase == prev_phase:
            return prev_phase, 0.0, "None"
        else:
            if prev_cand == cand_phase:
                new_counter = prev_counter + dt
            else:
                new_counter = dt

            if new_counter >= threshold:
                return cand_phase, 0.0, "None"
            else:
                return prev_phase, new_counter, cand_phase

    def update(self, dt):
        for i in range(bs.traf.ntraf):
            ac = self.model_refs[i]
            if ac is None:
                self.thrust[i] = self.drag[i] = self.fuelflow[i] = 0.0
                continue

            alt_m = bs.traf.alt[i]
            tas   = bs.traf.tas[i]
            vs    = bs.traf.vs[i]
            mass  = self.mass[i]
            ax    = bs.traf.ax[i]

            # Skip update at near-zero speed to prevent division by zero in the CL calculation
            if tas < 1.0:
                continue

            ap_alt = bs.traf.ap.alt[i] if (hasattr(bs.traf.ap, "alt") and i < len(bs.traf.ap.alt)) else alt_m
            phase, counter, cand = self._get_phase(
                vs, alt_m, ap_alt, self.phase[i],
                self.phase_counter[i], self.phase_cand[i], dt
            )
            self.phase[i]         = phase
            self.phase_counter[i] = counter
            self.phase_cand[i]    = cand

            T_actual  = bs.traf.Temp[i]
            deltaTemp = atm.ISATemperatureDeviation(
                temperature=T_actual,
                pressureAltitude=alt_m
            )

            theta_val = atm.theta(h=alt_m, deltaTemp=deltaTemp)
            delta_val = atm.delta(h=alt_m, deltaTemp=deltaTemp)
            sigma_val = atm.sigma(theta=theta_val, delta=delta_val)
            M = atm.tas2Mach(v=tas, theta=theta_val)

            try:
                config = ac.flightEnvelope.getConfig(
                    phase=phase, h=alt_m, mass=mass,
                    v=tas, deltaTemp=deltaTemp
                )

                CL = ac.flightEnvelope.CL(sigma=sigma_val, mass=mass, tas=tas)
                CD = ac.flightEnvelope.CD(CL=CL, config=config)
                D  = ac.flightEnvelope.D(sigma=sigma_val, tas=tas, CD=CD)
                self.drag[i] = D

                T_max  = ac.Thrust(
                    h=alt_m, deltaTemp=deltaTemp,
                    rating="MCMB", v=tas, config=config
                )
                T_idle = ac.Thrust(
                    h=alt_m, deltaTemp=deltaTemp,
                    rating="LIDL", v=tas, config=config
                )
                self.thr_max[i]  = T_max
                self.thr_idle[i] = T_idle

                if phase == "Climb":
                    T = T_max
                    # Uncomment the following two lines to apply the reduced power coefficient during the Climb phase
                    # Ccr = ac.reducedPower(h=alt_m, mass=mass, deltaTemp=deltaTemp)
                    # T = T_max * Ccr if Ccr is not None else T_max
                    
                elif phase == "Descent":
                    T = ac.TDes(
                        h=alt_m, deltaTemp=deltaTemp,
                        v=tas, config=config
                    )
                    T = max(T, T_idle)

                else:
                    T_mcrz = ac.Thrust(
                        h=alt_m, deltaTemp=deltaTemp,
                        rating="MCRZ", v=tas, config=config
                    )
                    # Clamp: ax<0 (decelerating) must not make thrust go below idle;
                    # ax>0 (accelerating) is capped at MCRZ to avoid overpowering.
                    T = float(np.clip(D + mass * ax, T_idle, T_mcrz))

                self.thrust[i] = T

                dT = T_max - T_idle
                bs.traf.thr[i] = float(np.clip((T - T_idle) / dT, 0.0, 1.0)) if dT > 0 else 0.0

                ff = ac.ff(
                    h=alt_m, v=tas, T=T,
                    config=config, flightPhase=phase
                )
                ff_min = ac.ffMin(h=alt_m)
                self.fuelflow[i] = max(ff, ff_min)

                vmin_ms = ac.flightEnvelope.VMin(
                    h=alt_m, mass=mass, config=config, deltaTemp=deltaTemp
                )
                vmax_ms = ac.flightEnvelope.VMax(h=alt_m, deltaTemp=deltaTemp)
                vstall  = ac.flightEnvelope.VStall(mass=mass, config=config)
                self.vmin[i]   = vmin_ms
                self.vmax[i]   = vmax_ms
                self.vstall[i] = vstall

            except Exception as exc:
                import traceback
                print(f"[PyBada3 ERROR] Exception in update: {exc}")
                traceback.print_exc()
                self.thrust[i]   = 0.0
                self.drag[i]     = 0.0
                self.fuelflow[i] = 0.0

    def limits(self, intent_v_tas, intent_vs, intent_h, ax):
        allow_v_tas = np.copy(intent_v_tas)
        allow_vs    = np.copy(intent_vs)
        allow_h     = np.copy(intent_h)

        from bluesky.tools.aero import cas2tas

        for i in range(bs.traf.ntraf):
            # Apply ceiling limits for actual aircraft models only.
            # Fallback models have artificially low ceilings that restrict realistic cruise altitudes.
            if not self.is_dummy[i]:
                if self.hmax[i] > 0:
                    allow_h[i] = min(allow_h[i], self.hmax[i])

            # Enforce Vmin and Vmax limits to prevent unrealistic speed spikes.
            # Values are converted from CAS to TAS before applying the limits.
            if self.vmin[i] > 0 and self.vmax[i] > 0:
                alt_m = bs.traf.alt[i]
                vmin_tas = cas2tas(self.vmin[i], alt_m)
                vmax_tas = cas2tas(self.vmax[i], alt_m)
                allow_v_tas[i] = np.clip(allow_v_tas[i], vmin_tas, vmax_tas)

        return allow_v_tas, allow_vs, allow_h


# ===========================================================================
# BADA 4 sub-model
# ===========================================================================

class PyBada4(PerfBase):
    """BADA 4 performance sub-model (extracted from pybadaperf4.py)."""

    BADA_DIR = "/home/paucr/bluesky/.venv/lib/python3.12/site-packages/pyBADA/aircraft/BADA4/DUMMY"
    BADA_VER = "4.2"

    FALLBACK_TYPE = "Dummy-TWIN"

    def __init__(self, parent: PyBadaPerf):
        type(self)._instance = self
        self._parent = parent
        self.cached_models = {}
        self.failed_models  = set()

        n = bs.traf.ntraf
        self.model_refs  = np.empty(n, dtype=object)
        self.phase       = np.empty(n, dtype=object)
        self.thr_idle    = np.zeros(n)
        self.thr_max     = np.zeros(n)
        self.vmin        = np.zeros(n)
        self.vmax        = np.zeros(n)
        self.vstall      = np.zeros(n)
        # is_dummy indicates if the aircraft uses the Dummy-TWIN fallback.
        # Ceiling limits are ignored for these models.
        self.is_dummy    = np.ones(n, dtype=bool)
        self.phase_counter = np.zeros(n)
        self.phase_cand    = np.full(n, 'None', dtype=object)

        self.thrust   = np.zeros(n)
        self.drag     = np.zeros(n)
        self.fuelflow = np.zeros(n)
        self.mass     = parent.mass.copy() if n > 0 else np.zeros(n)
        self.hmax     = parent.hmax.copy() if n > 0 else np.zeros(n)
        self.vsmin    = parent.vsmin.copy() if n > 0 else np.zeros(n)
        self.vsmax    = parent.vsmax.copy() if n > 0 else np.zeros(n)
        self.axmax    = parent.axmax.copy() if n > 0 else np.zeros(n)

        if n > 0:
            self.phase[:] = 'Climb'

        print(f"[PyBada4] Sub-model instantiated (BADA {self.BADA_VER})")

    def _grow(self, n):
        def _app(arr):
            return np.concatenate([arr, np.zeros(n)])

        self.model_refs  = np.concatenate([self.model_refs, np.empty(n, dtype=object)])
        self.phase       = np.concatenate([self.phase, np.full(n, 'Climb', dtype=object)])
        self.is_dummy    = np.concatenate([self.is_dummy, np.ones(n, dtype=bool)])
        self.phase_counter = np.concatenate([self.phase_counter, np.zeros(n)])
        self.phase_cand    = np.concatenate([self.phase_cand, np.full(n, 'None', dtype=object)])
        self.thr_idle    = _app(self.thr_idle)
        self.thr_max     = _app(self.thr_max)
        self.vmin        = _app(self.vmin)
        self.vmax        = _app(self.vmax)
        self.vstall      = _app(self.vstall)
        self.thrust      = _app(self.thrust)
        self.drag        = _app(self.drag)
        self.fuelflow    = _app(self.fuelflow)
        self.mass        = _app(self.mass)
        self.hmax        = _app(self.hmax)
        self.vsmin       = _app(self.vsmin)
        self.vsmax       = _app(self.vsmax)
        self.axmax       = _app(self.axmax)

    def create(self, n=1):
        self._grow(n)

        for i in range(bs.traf.ntraf - n, bs.traf.ntraf):
            actype = bs.traf.type[i].upper()
            self.phase[i]       = 'Climb'
            self.phase_counter[i] = 0.0
            self.phase_cand[i]    = 'None'

            if actype not in self.cached_models and actype not in self.failed_models:
                model, is_dummy = self._load_model(actype)
                self.cached_models[actype] = (model, is_dummy)

            entry = self.cached_models.get(actype, (None, True))
            ac, is_dummy = entry if isinstance(entry, tuple) else (entry, True)
            self.model_refs[i] = ac
            self.is_dummy[i]   = is_dummy

            if ac is not None:
                if self.mass[i] <= 0.0:
                    self.mass[i] = ac.AC.MREF
                mass = self.mass[i]
                try:
                    self._init_envelope(i, ac, mass)
                except Exception as exc:
                    print(f"[PyBada4] Envelope init failed for {actype}: {exc}")

    def _load_model(self, actype):
        import os

        names_to_try = [actype]
        ci_names = set()

        try:
            entries = os.listdir(self.BADA_DIR)
            ci_match = next(
                (e for e in entries if e.upper() == actype.upper() and e != actype), None
            )
            if ci_match:
                names_to_try.append(ci_match)
                ci_names.add(ci_match)
        except OSError:
            pass

        if self.FALLBACK_TYPE not in names_to_try:
            names_to_try.append(self.FALLBACK_TYPE)

        for name in names_to_try:
            try:
                model = Bada4Aircraft(
                    badaVersion=self.BADA_VER,
                    acName=name,
                    filePath=self.BADA_DIR,
                )
                is_dummy = (name != actype)
                if not is_dummy:
                    print(f"[PyBada4] Loaded BADA 4 model for {actype}")
                elif name in ci_names:
                    print(f"[PyBada4] Loaded {actype} as '{name}'")
                else:
                    print(f"[PyBada4] WARNING: {actype} not found in BADA data — "
                          f"using fallback '{self.FALLBACK_TYPE}' instead. "
                          f"Speed-envelope limits will NOT be applied. "
                          f"Install licensed BADA 4 data for accurate results.")
                return model, is_dummy
            except Exception as exc:
                if name == self.FALLBACK_TYPE and name not in ci_names:
                    print(f"[PyBada4] Fallback {self.FALLBACK_TYPE} also failed: {exc}")

        self.failed_models.add(actype)
        return None, True

    def _init_envelope(self, i, ac, mass):
        ref_h         = 1000.0
        ref_deltaTemp = 0.0
        ref_tas       = 250.0 / 1.94384

        theta_ref, delta_ref, sigma_ref = atm.atmosphereProperties(
            h=ref_h, deltaTemp=ref_deltaTemp
        )
        M_ref = atm.tas2Mach(v=ref_tas, theta=theta_ref)

        config_cr = ac.flightEnvelope.getConfig(
            phase="Cruise", h=ref_h, mass=mass,
            v=ref_tas, deltaTemp=ref_deltaTemp
        )
        HLid, LG = ac.flightEnvelope.getAeroConfig(config=config_cr)

        hmax_m = ac.flightEnvelope.maxAltitude(
            HLid=HLid, LG=LG, M=M_ref,
            deltaTemp=ref_deltaTemp, mass=mass
        )
        self.hmax[i] = hmax_m

        vmin_ms = ac.flightEnvelope.VMin(
            config=config_cr, theta=theta_ref, delta=delta_ref, mass=mass
        )
        vmax_ms = ac.flightEnvelope.VMax(
            h=ref_h, HLid=HLid, LG=LG,
            delta=delta_ref, theta=theta_ref, mass=mass
        )
        vstall = ac.flightEnvelope.VStall(
            mass=mass, HLid=HLid, LG=LG, theta=theta_ref, delta=delta_ref
        )

        self.vmin[i]   = vmin_ms
        self.vmax[i]   = vmax_ms
        self.vstall[i] = vstall
        self.vsmin[i]  = -6000 * 0.00508
        self.vsmax[i]  =  6000 * 0.00508
        # BADA 4 UM §5: typical jet transport ax_max ≈ 0.5 m/s²
        self.axmax[i]  = 0.5

    @staticmethod
    def _get_phase(vs, alt_m, ap_alt_m, prev_phase, prev_counter, prev_cand, dt):
        """Determines flight phase based on vertical speed and altitude.
        
        Uses time-persistence to prevent phase oscillations during altitude capture.
        """
        # -- unit conversions --
        alt_ft    = alt_m   * 3.28084
        ap_alt_ft = ap_alt_m * 3.28084
        vs_fpm    = vs * 60.0 * 3.28084

        # -- thresholds --
        MIN_CRUISE_ALT_FT     = 10000.0 # Cruise phase can only be selected above 10,000 ft
        TOC_ALT_BAND_FT       = 500.0   # Altitude capture band for TOC
        TOD_ALT_BAND_FT       = 500.0   # Altitude capture band for TOD
        CLIMB_VS_FPM          = 300.0   # VS to confirm climb/re-climb
        DESCENT_VS_FPM        = -300.0  # VS to confirm descent/re-descent
        LEVEL_VS_FPM          = 150.0   # VS threshold to consider level flight

        CRUISE_ACCUM_THRESHOLD = 90.0
        TRANSITION_ACCUM_THRESHOLD = 10.0

        alt_diff_ft = ap_alt_ft - alt_ft

        # Fallback initialization (if prev_phase is None or invalid)
        if prev_phase not in ("Climb", "Cruise", "Descent"):
            if vs_fpm > CLIMB_VS_FPM:
                return "Climb", 0.0, "None"
            elif vs_fpm < DESCENT_VS_FPM:
                return "Descent", 0.0, "None"
            else:
                return "Cruise", 0.0, "None"

        # Determine candidate phase
        cand_phase = prev_phase
        threshold = 0.0

        if prev_phase == "Climb":
            # Climb -> Cruise (TOC reached)
            cond_cruise = (alt_ft >= MIN_CRUISE_ALT_FT) and (
                (abs(alt_diff_ft) <= TOC_ALT_BAND_FT and abs(vs_fpm) <= LEVEL_VS_FPM) or vs_fpm < 50.0
            )
            # Climb -> Descent (Aborted climb / emergency descent)
            cond_descent = (vs_fpm < DESCENT_VS_FPM and alt_diff_ft < -TOD_ALT_BAND_FT)

            if cond_cruise:
                cand_phase = "Cruise"
                threshold = CRUISE_ACCUM_THRESHOLD
            elif cond_descent:
                cand_phase = "Descent"
                threshold = TRANSITION_ACCUM_THRESHOLD

        elif prev_phase == "Cruise":
            # Cruise -> Climb (Step Climb / Re-climb)
            cond_climb = (alt_diff_ft > TOC_ALT_BAND_FT and vs_fpm > CLIMB_VS_FPM)
            # Cruise -> Descent (TOD reached)
            cond_descent = (alt_diff_ft < -TOD_ALT_BAND_FT and vs_fpm < DESCENT_VS_FPM)

            if cond_climb:
                cand_phase = "Climb"
                threshold = TRANSITION_ACCUM_THRESHOLD
            elif cond_descent:
                cand_phase = "Descent"
                threshold = TRANSITION_ACCUM_THRESHOLD

        elif prev_phase == "Descent":
            # Descent -> Cruise (Level-off / intermediate hold)
            cond_cruise = (alt_ft >= MIN_CRUISE_ALT_FT) and (
                abs(alt_diff_ft) <= TOD_ALT_BAND_FT and abs(vs_fpm) <= LEVEL_VS_FPM
            )
            # Descent -> Climb (Go-around / re-climb)
            cond_climb = (vs_fpm > CLIMB_VS_FPM and alt_diff_ft > TOC_ALT_BAND_FT)

            if cond_cruise:
                cand_phase = "Cruise"
                threshold = CRUISE_ACCUM_THRESHOLD
            elif cond_climb:
                cand_phase = "Climb"
                threshold = TRANSITION_ACCUM_THRESHOLD

        # State transition logic
        if cand_phase == prev_phase:
            return prev_phase, 0.0, "None"
        else:
            if prev_cand == cand_phase:
                new_counter = prev_counter + dt
            else:
                new_counter = dt

            if new_counter >= threshold:
                return cand_phase, 0.0, "None"
            else:
                return prev_phase, new_counter, cand_phase

    def update(self, dt):
        for i in range(bs.traf.ntraf):
            ac = self.model_refs[i]
            if ac is None:
                self.thrust[i] = self.drag[i] = self.fuelflow[i] = 0.0
                continue

            alt_m = bs.traf.alt[i]
            tas   = bs.traf.tas[i]
            vs    = bs.traf.vs[i]
            mass  = self.mass[i]
            ax    = bs.traf.ax[i]

            # Skip update at near-zero speed to prevent division by zero in the CL calculation
            if tas < 1.0:
                continue

            ap_alt = bs.traf.ap.alt[i] if (hasattr(bs.traf.ap, "alt") and i < len(bs.traf.ap.alt)) else alt_m
            phase, counter, cand = self._get_phase(
                vs, alt_m, ap_alt, self.phase[i],
                self.phase_counter[i], self.phase_cand[i], dt
            )
            self.phase[i]         = phase
            self.phase_counter[i] = counter
            self.phase_cand[i]    = cand

            T_actual  = bs.traf.Temp[i]
            deltaTemp = atm.ISATemperatureDeviation(
                temperature=T_actual,
                pressureAltitude=alt_m
            )

            theta_val = atm.theta(h=alt_m, deltaTemp=deltaTemp)
            delta_val = atm.delta(h=alt_m, deltaTemp=deltaTemp)
            M = atm.tas2Mach(v=tas, theta=theta_val)

            try:
                config = ac.flightEnvelope.getConfig(
                    phase=phase, h=alt_m, mass=mass,
                    v=tas, deltaTemp=deltaTemp
                )
                HLid, LG = ac.flightEnvelope.getAeroConfig(config=config)

                CL = ac.flightEnvelope.CL(delta=delta_val, mass=mass, M=M)
                CD = ac.flightEnvelope.CD(HLid=HLid, LG=LG, CL=CL, M=M)
                D  = ac.flightEnvelope.D(delta=delta_val, M=M, CD=CD)
                self.drag[i] = D

                T_max  = ac.flightEnvelope.Thrust(
                    delta=delta_val, theta=theta_val, M=M, deltaTemp=deltaTemp, rating="MCMB"
                )
                T_idle = ac.flightEnvelope.Thrust(
                    delta=delta_val, theta=theta_val, M=M, deltaTemp=deltaTemp, rating="LIDL"
                )
                T_idle = max(T_idle, 0.0)
                self.thr_max[i]  = T_max
                self.thr_idle[i] = T_idle

                if phase == "Climb":
                    T         = T_max
                    ff_rating = "MCMB"
                    CT        = None

                elif phase == "Descent":
                    T         = T_idle
                    ff_rating = "LIDL"
                    CT        = None

                else:
                    T_mcrz = ac.flightEnvelope.Thrust(
                        delta=delta_val, theta=theta_val, M=M, deltaTemp=deltaTemp, rating="MCRZ"
                    )
                    # Clamp: ax<0 (decelerating) must not make thrust go below idle;
                    # ax>0 (accelerating) is capped at MCRZ to avoid overpowering.
                    T         = float(np.clip(D + mass * ax, T_idle, T_mcrz))
                    CT        = ac.flightEnvelope.CT(delta=delta_val, Thrust=T)
                    ff_rating = None

                self.thrust[i] = T

                dT = T_max - T_idle
                bs.traf.thr[i] = float(np.clip((T - T_idle) / dT, 0.0, 1.0)) if dT > 0 else 0.0

                if ff_rating is not None:
                    ff = ac.flightEnvelope.ff(
                        delta=delta_val, theta=theta_val, deltaTemp=deltaTemp,
                        rating=ff_rating, M=M, config=config
                    )
                    ff_idle = ac.flightEnvelope.ff(
                        delta=delta_val, theta=theta_val, deltaTemp=deltaTemp,
                        rating="LIDL", M=M, config=config
                    )
                else:
                    ff = ac.flightEnvelope.ff(
                        delta=delta_val, theta=theta_val, deltaTemp=deltaTemp,
                        CT=CT, M=M, config=config
                    )
                    ff_idle = ac.flightEnvelope.ff(
                        delta=delta_val, theta=theta_val, deltaTemp=deltaTemp,
                        rating="LIDL", M=M, config=config
                    )
                self.fuelflow[i] = max(ff, ff_idle)

                vmin_ms = ac.flightEnvelope.VMin(
                    config=config, theta=theta_val, delta=delta_val, mass=mass
                )
                vmax_ms = ac.flightEnvelope.VMax(
                    h=alt_m, HLid=HLid, LG=LG,
                    delta=delta_val, theta=theta_val, mass=mass
                )
                vstall = ac.flightEnvelope.VStall(
                    mass=mass, HLid=HLid, LG=LG, theta=theta_val, delta=delta_val
                )
                self.vmin[i]   = vmin_ms
                self.vmax[i]   = vmax_ms
                self.vstall[i] = vstall

            except Exception as exc:
                import traceback
                print(f"[PyBada4 ERROR] Exception in update for {bs.traf.type[i]} (idx {i}): {exc}")
                traceback.print_exc()
                self.thrust[i]   = 0.0
                self.drag[i]     = 0.0
                self.fuelflow[i] = 0.0

    def limits(self, intent_v_tas, intent_vs, intent_h, ax):
        allow_v_tas = np.copy(intent_v_tas)
        allow_vs    = np.copy(intent_vs)
        allow_h     = np.copy(intent_h)

        from bluesky.tools.aero import cas2tas

        for i in range(bs.traf.ntraf):
            # Apply ceiling limits for actual aircraft models only.
            # Fallback models have artificially low ceilings that restrict realistic cruise altitudes.
            if not self.is_dummy[i]:
                if self.hmax[i] > 0:
                    allow_h[i] = min(allow_h[i], self.hmax[i])

            # Enforce Vmin and Vmax limits to prevent unrealistic speed spikes.
            # Values are converted from CAS to TAS before applying the limits.
            if self.vmin[i] > 0 and self.vmax[i] > 0:
                alt_m = bs.traf.alt[i]
                vmin_tas = cas2tas(self.vmin[i], alt_m)
                vmax_tas = cas2tas(self.vmax[i], alt_m)
                allow_v_tas[i] = np.clip(allow_v_tas[i], vmin_tas, vmax_tas)

        return allow_v_tas, allow_vs, allow_h
