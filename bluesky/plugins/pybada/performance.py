"""BlueSky performance implementation for longitudinal/vertical TEM."""

from pathlib import Path

import numpy as np

import bluesky as bs
from bluesky.traffic.performance.perfbase import PerfBase
from .model import EnergyResult, EvaluationError, ModelStore, ModelUnavailable


bs.settings.set_variable_defaults(
    pybada3_data_path='', pybada4_data_path='', pybada_family='4',
    pybada_strict=False, pybada_aircraft_aliases={}, pybada_speed_schedule='ICAO')


class PyBadaTEM(PerfBase):
    """One authoritative BADA 3/4 integration with native lateral guidance."""

    def __init__(self):
        super().__init__()
        self.family = str(bs.settings.pybada_family)
        self.schedule = str(bs.settings.pybada_speed_schedule).upper()
        self.strict = bool(bs.settings.pybada_strict)
        self.store = None
        self.models = []
        self.resolutions = []
        with self.settrafarrays():
            self.dyn_mode = np.array([], dtype=int)
            self.rated_thrust = np.array([])
            self.mass_override = np.array([], dtype=bool)
            self.invalid = np.array([], dtype=bool)
            self.failure_count = np.array([], dtype=int)

    @staticmethod
    def _discover(family):
        configured = bs.settings.pybada3_data_path if family == '3' else bs.settings.pybada4_data_path
        if configured:
            return configured
        try:
            package = __import__('pyBADA')
        except ImportError as exc:
            raise ModelUnavailable('Install the pybada optional dependency before loading PYBADATEM') from exc
        base = Path(package.__file__).resolve().parent / 'aircraft' / ('BADA3' if family == '3' else 'BADA4')
        dummy = base / 'DUMMY'
        return str(dummy if dummy.is_dir() else base)

    def activate(self, family=None):
        family = str(family or self.family).replace('BADA', '')
        if family not in ('3', '4'):
            raise ValueError('PERFMODEL accepts BADA3 or BADA4')
        self.store = ModelStore(family, self._discover(family),
                                bs.settings.pybada_aircraft_aliases, self.strict)
        self.family = family
        self.models.clear()
        self.resolutions.clear()
        for actype in bs.traf.type:
            model, resolution = self.store.resolve(actype)
            self.models.append(model)
            self.resolutions.append(resolution)

    def create(self, n):
        super().create(n)
        self.dyn_mode[-n:] = 1
        if self.store is None:
            self.activate()
        for actype in bs.traf.type[-n:]:
            model, resolution = self.store.resolve(actype)
            self.models.append(model)
            self.resolutions.append(resolution)
        for i in range(len(self.mass) - n, len(self.mass)):
            self.mass[i] = float(getattr(self.models[i], 'MREF', getattr(self.models[i], 'OEW', 60000.0)))

    def validate_create(self, actypes):
        """Resolve every requested model before BlueSky creates any aircraft."""
        if self.store is None:
            self.activate()
        try:
            for actype in actypes:
                self.store.resolve(actype)
        except ModelUnavailable as exc:
            return False, str(exc)
        return True, ''

    def delete(self, idx):
        if np.isscalar(idx):
            idxs = [int(idx)]
        else:
            idxs = sorted((int(i) for i in idx), reverse=True)
        for i in idxs:
            del self.models[i]
            del self.resolutions[i]
        super().delete(idx)

    def reset(self):
        self.models.clear()
        self.resolutions.clear()
        super().reset()

    def _evaluate(self, idx):
        """Evaluate the pyBADA API through one observable failure boundary."""
        ac = self.models[idx]
        h, tas, mass = bs.traf.pressure_alt[idx], bs.traf.tas[idx], self.mass[idx]
        phase = 'Climb' if bs.traf.aporasas.alt[idx] > bs.traf.alt[idx] + 1.0 else \
            ('Descent' if bs.traf.aporasas.alt[idx] < bs.traf.alt[idx] - 1.0 else 'Cruise')
        try:
            # Adapter-friendly hook used by dependency-free fakes and future
            # pyBADA-version-specific adapters.
            if hasattr(ac, 'bluesky_energy'):
                return EnergyResult(**ac.bluesky_energy(h=h, tas=tas, mass=mass,
                    temperature=bs.traf.Temp[idx], pressure=bs.traf.p[idx], phase=phase,
                    schedule=self.schedule)).validate()
            raise EvaluationError('Installed pyBADA model needs a version-specific bluesky_energy adapter')
        except Exception as exc:
            raise EvaluationError(
                f'{bs.traf.id[idx]}/{bs.traf.type[idx]} BADA{self.family} h={h:.1f} '
                f'TAS={tas:.3f} mass={mass:.1f} phase={phase} schedule={self.schedule}: {exc}') from exc

    def update_dynamics(self, traffic, dt):
        handled = np.zeros(traffic.ntraf, dtype=bool)
        # Performance is evaluated for every aircraft, like BlueSky's original
        # BADA implementation.  dyn_mode only decides whether those results
        # drive motion; KINEMATIC runs still retain usable performance/fuel data.
        for idx in range(traffic.ntraf):
            try:
                result = self._evaluate(idx)
                self.thrust[idx], self.rated_thrust[idx], self.drag[idx], self.fuelflow[idx] = \
                    result.thrust, result.rated_thrust, result.drag, result.fuel_flow
                self.mass[idx] = max(1.0, self.mass[idx] - result.fuel_flow * dt)
                if self.dyn_mode[idx] == 1:
                    traffic.ax[idx] = result.acceleration
                    traffic.tas[idx] = max(0.0, traffic.tas[idx] + result.acceleration * dt)
                    delta_alt = traffic.aporasas.alt[idx] - traffic.alt[idx]
                    traffic.vs[idx] = np.sign(delta_alt) * min(abs(result.rocd), abs(delta_alt) / dt)
                    handled[idx] = True
                self.invalid[idx] = False
            except (ModelUnavailable, EvaluationError) as exc:
                self.invalid[idx] = True
                self.failure_count[idx] += 1
                self.thrust[idx] = self.rated_thrust[idx] = self.drag[idx] = self.fuelflow[idx] = np.nan
                if self.strict:
                    raise RuntimeError(f'PYBADATEM strict evaluation failure: {exc}') from exc
        return handled
