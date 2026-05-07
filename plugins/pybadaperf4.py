import numpy as np
import bluesky as bs
from bluesky.traffic.performance.perfbase import PerfBase
import pyBADA.atmosphere as atm
from pyBADA.bada4 import Bada4Aircraft


def init_plugin():

    PyBada4.select()

    #missatge per comprobar
    print("=" * 60)
    print("  PERFORMANCE MODEL : pyBADA 4")
    print(f"  BADA version      : {PyBada4.BADA_VER}")
    print(f"  Data directory    : {PyBada4.BADA_DIR}")
    print("=" * 60)

    return {
        'plugin_name': 'PYBADAPERF4',
        'plugin_type': 'sim',
    }


class PyBada4(PerfBase):

    BADA_DIR = "/home/paucr/bluesky/.venv/lib/python3.12/site-packages/pyBADA/aircraft/BADA4/DUMMY"
    BADA_VER = "4.2"

    # Si no troba el model posa twin
    FALLBACK_TYPE = "Dummy-TWIN"

    # VS thresholds per la fase d vol
    VS_CLIMB   =  0.5
    VS_DESCENT = -0.5

    def __init__(self):
        super().__init__()
        self.cached_models = {}    
        self.failed_models  = set()

        with self.settrafarrays():
            self.model_refs = np.array([], dtype=object)
            self.phase      = np.array([], dtype=object)   # "Climb"/"Cruise"/"Descent"
            self.thr_idle   = np.array([])    # LIDL thrust  
            self.thr_max    = np.array([])    # MCMB thrust  
            self.lift       = np.array([])    # Lift force   
            self.vs_bada    = np.array([])    # BADA ROCD     
            self.vmin       = np.array([])    # Min CAS      
            self.vmax       = np.array([])    # Max CAS      
            self.vstall     = np.array([])    # Stall CAS    

        print(f"[PyBada4] Performance model instantiated (BADA {self.BADA_VER})")

    def create(self, n=1):
        super().create(n)

        for i in range(bs.traf.ntraf - n, bs.traf.ntraf):
            actype = bs.traf.type[i].upper()

            # pq no sigui mai None
            self.phase[i] = 'Cruise'

            # Carrega el model si no està a la cachce
            if actype not in self.cached_models and actype not in self.failed_models:
                model = self._load_model(actype)
                self.cached_models[actype] = model

            self.model_refs[i] = self.cached_models.get(actype)

            ac = self.model_refs[i]
            if ac is not None:
                mass = self.mass[i]
                try:
                    self._init_envelope(i, ac, mass)
                except Exception as exc:
                    print(f"[PyBada4] Envelope init failed for {actype}: {exc}")

    # Funció del  claude x trobar el nom del model encara q estigui mal escrit. Es pot treure.

    def _load_model(self, actype):
        import os

        # construïm la llista de noms a provar:
        # 1) el nom exacte (uppercase), 2) coincidència case-insensitive al directori, 3) fallback genèric
        names_to_try = [actype]
        ci_names = set()   # noms trobats via cerca case-insensitive (no exacta)

        # cerca case-insensitive: si al directori hi ha un nom que coincideix
        # amb actype ignorant majúscules, l'afegim com a segon intent
        try:
            entries = os.listdir(self.BADA_DIR)
            ci_match = next((e for e in entries if e.upper() == actype.upper() and e != actype), None)
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
                if name == actype:
                    print(f"[PyBada4] Loaded BADA 4 model for {actype}")
                elif name in ci_names:
                    # trobat al directori amb diferent capitalització, no és error
                    print(f"[PyBada4] Loaded {actype} as '{name}' (case corrected)")
                else:
                    print(f"[PyBada4] {actype} not found — using fallback {self.FALLBACK_TYPE}")
                return model
            except Exception as exc:
                if name == self.FALLBACK_TYPE and name not in ci_names:
                    print(f"[PyBada4] Fallback {self.FALLBACK_TYPE} also failed: {exc}")

        self.failed_models.add(actype)
        return None


    def _init_envelope(self, i, ac, mass):
        #Calcula els limits estàtics
        ref_h         = 1000.0
        ref_deltaTemp = 0.0
        ref_tas       = 250.0 / 1.94384   # ≈ 250 kt

        theta_ref, delta_ref, sigma_ref = atm.atmosphereProperties(
            h=ref_h, deltaTemp=ref_deltaTemp
        )
        M_ref = atm.tas2Mach(v=ref_tas, theta=theta_ref)

        config_cr = ac.flightEnvelope.getConfig(
            phase="Cruise", h=ref_h, mass=mass,
            v=ref_tas, deltaTemp=ref_deltaTemp
        )
        HLid, LG = ac.flightEnvelope.getAeroConfig(config=config_cr)

        # sostre màxim (ceiling)
        hmax_m = ac.flightEnvelope.maxAltitude(
            HLid=HLid, LG=LG, M=M_ref,
            deltaTemp=ref_deltaTemp, mass=mass
        )
        self.hmax[i] = hmax_m

        # Speed envelope
        vmin_ms = ac.flightEnvelope.VMin(
            config=config_cr, theta=theta_ref, delta=delta_ref, mass=mass
        )
        vmax_ms = ac.flightEnvelope.VMax(
            h=ref_h, HLid=HLid, LG=LG,
            delta=delta_ref, theta=theta_ref, mass=mass
        )
        vstall = ac.flightEnvelope.VStall(mass=mass, HLid=HLid, LG=LG, theta=theta_ref, delta=delta_ref)

        self.vmin[i]   = vmin_ms
        self.vmax[i]   = vmax_ms
        self.vstall[i] = vstall

        self.vsmin[i] = -6000 * 0.00508   # ≈ −30.5 m/s
        self.vsmax[i] =  6000 * 0.00508   # ≈ +30.5 m/s
        self.axmax[i] = 2.0

    @staticmethod
    def _get_phase(vs):
        if vs > PyBada4.VS_CLIMB:
            return "Climb"
        elif vs < PyBada4.VS_DESCENT:
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

            # desviacó de temperatura respecte ISA
            T_actual  = bs.traf.Temp[i]
            deltaTemp = atm.ISATemperatureDeviation(
                temperature=T_actual,
                pressureAltitude=alt_m
            )

            # ratios d atmosfera
            theta_val = atm.theta(h=alt_m, deltaTemp=deltaTemp)
            delta_val = atm.delta(h=alt_m, deltaTemp=deltaTemp)
            M = atm.tas2Mach(v=tas, theta=theta_val)

            try:
                # Aerodynamic configuration
                config = ac.flightEnvelope.getConfig(
                    phase=phase, h=alt_m, mass=mass,
                    v=tas, deltaTemp=deltaTemp
                )
                HLid, LG = ac.flightEnvelope.getAeroConfig(config=config)

                # Forces aerodinàmiques
                CL = ac.flightEnvelope.CL(delta=delta_val, mass=mass, M=M)
                CD = ac.flightEnvelope.CD(HLid=HLid, LG=LG, CL=CL, M=M)
                D  = ac.flightEnvelope.D(delta=delta_val, M=M, CD=CD)
                L  = ac.flightEnvelope.L(delta=delta_val, M=M, CL=CL)
                self.drag[i] = D
                self.lift[i] = L

                # coeficients de thrust i límits de cada rating:
                # CT_nonLIDL és per MCMB (pujada màxima) i MCRZ (creuer)
                # CT_LIDL és per flight-idle (baixada sense potència)
                # Thrust [N] = CT * delta * p0 * S_ref
                CT_max  = ac.flightEnvelope.CT_nonLIDL(
                    theta=theta_val, delta=delta_val, M=M, rating="MCMB"
                )
                CT_idle = ac.flightEnvelope.CT_LIDL(
                    delta=delta_val, theta=theta_val, M=M
                )
                T_max  = ac.flightEnvelope.Thrust(delta=delta_val, CT=CT_max)
                T_idle = ac.flightEnvelope.Thrust(delta=delta_val, CT=CT_idle)
                self.thr_max[i]  = T_max
                self.thr_idle[i] = T_idle

                # selecció del thrust depenent de la fase
                if phase == "Climb":
                    CT = CT_max
                    T  = T_max

                elif phase == "Descent":
                    CT = CT_idle
                    T  = T_idle

                else:
                    CT_mcrz = ac.flightEnvelope.CT_nonLIDL(
                        theta=theta_val, delta=delta_val, M=M, rating="MCRZ"
                    )
                    T_mcrz = ac.flightEnvelope.Thrust(delta=delta_val, CT=CT_mcrz)
                    T = min(D + mass * ax, T_mcrz)
                    # CT per calcular el consum
                    CT = ac.flightEnvelope.CT(delta=delta_val, Thrust=T)

                self.thrust[i] = T

                # throttle normalitzat: 0 = idle, 1 = max
                dT = T_max - T_idle
                bs.traf.thr[i] = float(np.clip((T - T_idle) / dT, 0.0, 1.0)) if dT > 0 else 0.0

                # Energy share factor & ROCD 
                ESF = ac.flightEnvelope.esf(
                    h=alt_m, deltaTemp=deltaTemp,
                    flightEvolution="constCAS",
                    phase=phase, v=tas, vdes=None
                )
                rocd = ac.flightEnvelope.ROCD(
                    T=T, D=D, v=tas, mass=mass,
                    ESF=ESF, h=alt_m, deltaTemp=deltaTemp
                )
                self.vs_bada[i] = rocd

                # BADA 4 calcula el fuel flow amb CT i M (no TAS) i els inputs atmosfèrics
                ff = ac.flightEnvelope.ff(
                    delta=delta_val,
                    theta=theta_val,
                    deltaTemp=deltaTemp,
                    CT=CT,
                    M=M,
                    config=config
                )
                # consum mínim 
                ff_idle = ac.flightEnvelope.ff(
                    delta=delta_val,
                    theta=theta_val,
                    deltaTemp=deltaTemp,
                    CT=CT_idle,
                    M=M,
                    config=config
                )
                self.fuelflow[i] = max(ff, ff_idle)

                # update de les velocitats
                vmin_ms = ac.flightEnvelope.VMin(
                    config=config, theta=theta_val, delta=delta_val, mass=mass
                )
                vmax_ms = ac.flightEnvelope.VMax(
                    h=alt_m, HLid=HLid, LG=LG,
                    delta=delta_val, theta=theta_val, mass=mass
                )
                vstall = ac.flightEnvelope.VStall(mass=mass, HLid=HLid, LG=LG, theta=theta_val, delta=delta_val)
                self.vmin[i]   = vmin_ms
                self.vmax[i]   = vmax_ms
                self.vstall[i] = vstall

            except Exception as exc:
                self.thrust[i]   = 0.0
                self.drag[i]     = 0.0
                self.fuelflow[i] = 0.0


    # Forçar limits


    def limits(self, intent_v_tas, intent_vs, intent_h, ax):
        allow_v_tas = np.copy(intent_v_tas)
        allow_vs    = np.copy(intent_vs)
        allow_h     = np.copy(intent_h)

        for i in range(bs.traf.ntraf):
            # Altitude ceiling
            if self.hmax[i] > 0:
                allow_h[i] = min(allow_h[i], self.hmax[i])
            # speed limits
            if self.vmin[i] > 0 and self.vmax[i] > 0:
                allow_v_tas[i] = np.clip(allow_v_tas[i], self.vmin[i], self.vmax[i])

        return allow_v_tas, allow_vs, allow_h
