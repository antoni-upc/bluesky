import numpy as np
import bluesky as bs
from bluesky.traffic.performance.perfbase import PerfBase
import pyBADA.atmosphere as atm
from pyBADA.bada3 import Bada3Aircraft
from pyBADA import constants as const


def init_plugin():

    PyBada3.select()
    #missatge per comprobar
    print("=" * 60)
    print("  PERFORMANCE MODEL : pyBADA 3")
    print(f"  BADA version      : {PyBada3.BADA_VER}")
    print(f"  Data directory    : {PyBada3.BADA_DIR}")
    print("=" * 60)

    return {
        'plugin_name': 'PYBADAPERF3',
        'plugin_type': 'sim',
    }

class PyBada3(PerfBase):

    BADA_DIR = "/home/paucr/bluesky/.venv/lib/python3.12/site-packages/pyBADA/aircraft/BADA3/DUMMY"
    BADA_VER = "3.15"

    # VS thresholds per la fase d vol
    VS_CLIMB   =  0.5
    VS_DESCENT = -0.5

    def __init__(self):
        super().__init__()
        self.cached_models = {}
        self.failed_models  = set()

        with self.settrafarrays():
            self.model_refs = np.array([], dtype=object)
            self.phase      = np.array([], dtype=object)   # "Climb" / "Cruise" / "Descent"
            self.thr_idle   = np.array([])    # LIDL thrust  
            self.thr_max    = np.array([])    # MCMB thrust  
            self.lift       = np.array([])    # Lift force   
            self.vs_bada    = np.array([])    # BADA ROCD   
            self.vmin       = np.array([])    # Min CAS 
            self.vmax       = np.array([])    # Max CAS 
            self.vstall     = np.array([])    # Stall CAS   

        print(f"[PyBada3] Performance model instantiated (BADA {self.BADA_VER})")


    def create(self, n=1):
        super().create(n)

        for i in range(bs.traf.ntraf - n, bs.traf.ntraf):
            actype = bs.traf.type[i].upper()

            # perque no sigui mai None
            self.phase[i] = 'Cruise'

            if actype not in self.cached_models and actype not in self.failed_models:
                try:
                    model = Bada3Aircraft(
                        badaVersion=self.BADA_VER,
                        acName=actype,
                        filePath=self.BADA_DIR,
                    )
                    self.cached_models[actype] = model
                    print(f"[PyBada3] Loaded BADA 3 model for {actype}")
                except Exception as exc:
                    print(f"[PyBada3] Failed to load BADA 3 model for {actype}: {exc}")
                    self.failed_models.add(actype)
                    self.cached_models[actype] = None

            self.model_refs[i] = self.cached_models.get(actype)

            ac = self.model_refs[i]
            if ac is not None:
                mass = self.mass[i]
                try:
                    # funcio de pyBada x sostre
                    self.hmax[i] = ac.flightEnvelope.maxAltitude(
                        mass=mass, deltaTemp=0.0
                    )

                    # velocitats
                    config_cr = ac.flightEnvelope.getConfig(
                        phase="Cruise", h=1000.0, mass=mass,
                        v=250.0 / 1.94384, deltaTemp=0.0 # ~250 kt TAS
                    )
                    vmin_ms = ac.flightEnvelope.VMin(
                        h=1000.0, mass=mass, config=config_cr, deltaTemp=0.0
                    )
                    vmax_ms = ac.flightEnvelope.VMax(h=1000.0, deltaTemp=0.0)
                    vstall  = ac.flightEnvelope.VStall(mass=mass, config=config_cr)

                    self.vmin[i]   = vmin_ms
                    self.vmax[i]   = vmax_ms
                    self.vstall[i] = vstall

                    # limits de BlueSky
                    self.vsmin[i] = -6000 * 0.00508
                    self.vsmax[i] =  6000 * 0.00508
                    self.axmax[i] = 2.0

                except Exception as exc:
                    print(f"[PyBada3] Envelope init failed for {actype}: {exc}")

    @staticmethod
    def _get_phase(vs):
        if vs > PyBada3.VS_CLIMB:
            return "Climb"
        elif vs < PyBada3.VS_DESCENT:
            return "Descent"
        return "Cruise"

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

            phase = self._get_phase(vs)
            self.phase[i] = phase

            T_actual = bs.traf.Temp[i]          
            deltaTemp = atm.ISATemperatureDeviation(
                temperature=T_actual,
                pressureAltitude=alt_m
            )

            theta_val = atm.theta(h=alt_m, deltaTemp=deltaTemp)
            delta_val = atm.delta(h=alt_m, deltaTemp=deltaTemp)
            sigma_val = atm.sigma(theta=theta_val, delta=delta_val)

            # Mach
            M = atm.tas2Mach(v=tas, theta=theta_val)

            try:
                config = ac.flightEnvelope.getConfig(
                    phase=phase, h=alt_m, mass=mass,
                    v=tas, deltaTemp=deltaTemp
                )

                CL = ac.flightEnvelope.CL(sigma=sigma_val, mass=mass, tas=tas)
                CD = ac.flightEnvelope.CD(CL=CL, config=config)
                D  = ac.flightEnvelope.D(sigma=sigma_val, tas=tas, CD=CD)
                L  = ac.flightEnvelope.L(sigma=sigma_val, tas=tas, CL=CL)
                self.drag[i] = D
                self.lift[i] = L

                # limits del Thrust
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

                # Thrust en funció de la fase
                if phase == "Climb":
                    Ccr = ac.reducedPower(h=alt_m, mass=mass, deltaTemp=deltaTemp)
                    T = T_max * Ccr if Ccr is not None else T_max

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
                    T = min(D + mass * ax, T_mcrz)

                self.thrust[i] = T

                # ── Energy share factor & ROCD (diagnostic) ────────
                ESF = ac.esf(
                    h=alt_m, deltaTemp=deltaTemp,
                    flightEvolution="constCAS",
                    phase=phase, v=tas, vdes=None
                )
                rocd = ac.ROCD(
                    T=T, D=D, v=tas, mass=mass,
                    ESF=ESF, h=alt_m, deltaTemp=deltaTemp
                )
                self.vs_bada[i] = rocd

                ff = ac.ff(
                    h=alt_m, v=tas, T=T,
                    config=config, flightPhase=phase
                )
                
                ff_min = ac.ffMin(h=alt_m)
                self.fuelflow[i] = max(ff, ff_min)

                # limits de velocitat
                vmin_ms = ac.flightEnvelope.VMin(
                    h=alt_m, mass=mass, config=config, deltaTemp=deltaTemp
                )
                vmax_ms = ac.flightEnvelope.VMax(h=alt_m, deltaTemp=deltaTemp)
                vstall  = ac.flightEnvelope.VStall(mass=mass, config=config)
                self.vmin[i]   = vmin_ms
                self.vmax[i]   = vmax_ms
                self.vstall[i] = vstall

            except Exception as exc:
                self.thrust[i]   = 0.0
                self.drag[i]     = 0.0
                self.fuelflow[i] = 0.0

    # Forçar els limits

    def limits(self, intent_v_tas, intent_vs, intent_h, ax):

        allow_v_tas = np.copy(intent_v_tas)
        allow_vs    = np.copy(intent_vs)
        allow_h     = np.copy(intent_h)

        for i in range(bs.traf.ntraf):
            # Altitude ceiling
            if self.hmax[i] > 0:
                allow_h[i] = min(allow_h[i], self.hmax[i])

            # Speed envelope
            if self.vmin[i] > 0 and self.vmax[i] > 0:
                allow_v_tas[i] = np.clip(allow_v_tas[i], self.vmin[i], self.vmax[i])

        return allow_v_tas, allow_vs, allow_h