"""Model resolution and typed pyBADA evaluation boundaries."""

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

import numpy as np


class ModelUnavailable(RuntimeError):
    pass


class EvaluationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Resolution:
    requested: str
    resolved: str
    method: str
    dummy: bool


@dataclass(frozen=True)
class EnergyResult:
    thrust: float
    rated_thrust: float
    drag: float
    fuel_flow: float
    esf: float
    rocd: float
    acceleration: float

    def validate(self):
        values = np.asarray((self.thrust, self.rated_thrust, self.drag,
                             self.fuel_flow, self.esf, self.rocd,
                             self.acceleration), dtype=float)
        if not np.all(np.isfinite(values)) or self.fuel_flow < 0.0:
            raise EvaluationError(f'Non-physical pyBADA result: {values!r}')
        return self


class BadaModelAdapter:
    """Narrow compatibility adapter for the supported pyBADA API family."""

    def __init__(self, model, family):
        self.model = model
        self.family = family

    def __getattr__(self, name):
        return getattr(self.model, name)

    @staticmethod
    def _atmosphere(h, tas, temperature):
        atm = import_module('pyBADA.atmosphere')
        dtemp = atm.ISATemperatureDeviation(temperature=temperature, pressureAltitude=h)
        theta = atm.theta(h=h, deltaTemp=dtemp)
        delta = atm.delta(h=h, deltaTemp=dtemp)
        sigma = atm.sigma(theta=theta, delta=delta)
        mach = atm.tas2Mach(v=tas, theta=theta)
        return atm, dtemp, theta, delta, sigma, mach

    def bluesky_energy(self, *, h, tas, mass, temperature, pressure, phase, schedule):
        ac = self.model
        atm, dtemp, theta, delta, sigma, mach = self._atmosphere(h, tas, temperature)
        bada_phase = {'Climb': 'cl', 'Descent': 'des'}.get(phase)
        evolution = 'constCAS' if schedule == 'CONSCAS' or h <= 9144.0 else 'constM'
        if self.family == '3':
            cas = atm.tas2Cas(tas=tas, delta=delta, sigma=sigma)
            config = ac.flightEnvelope.getConfig(phase=phase, h=h, mass=mass,
                                                  v=cas, deltaTemp=dtemp) or 'CR'
            lift = ac.flightEnvelope.CL(sigma=sigma, mass=mass, tas=tas)
            drag = ac.flightEnvelope.D(sigma=sigma, tas=tas,
                CD=ac.flightEnvelope.CD(CL=lift, config=config))
            rating = 'MCMB' if phase == 'Climb' else ('LIDL' if phase == 'Descent' else 'MCRZ')
            rated_thrust = ac.Thrust(h=h, deltaTemp=dtemp, rating=rating,
                                     v=tas, config=config)
            thrust = drag if phase == 'Cruise' else rated_thrust
            fuel = max(ac.ff(h=h, v=tas, T=thrust, config=config, flightPhase=phase), ac.ffMin(h=h))
            airplane = import_module('pyBADA.aircraft').Airplane
            esf = airplane.esf(flightEvolution=evolution, h=h, M=mach, deltaTemp=dtemp) \
                if bada_phase else 1.0
            rocd = ac.ROCD(thrust, drag, tas, mass, esf, h, dtemp) if bada_phase else 0.0
        else:
            cas = atm.tas2Cas(tas=tas, delta=delta, sigma=sigma)
            config = ac.flightEnvelope.getConfig(phase=phase, h=h, mass=mass,
                                                  v=cas, deltaTemp=dtemp) or 'CR'
            hlid, gear = ac.flightEnvelope.getAeroConfig(config=config)
            lift = ac.flightEnvelope.CL(delta=delta, mass=mass, M=mach)
            drag = ac.flightEnvelope.D(delta=delta, M=mach,
                CD=ac.flightEnvelope.CD(HLid=hlid, LG=gear, CL=lift, M=mach))
            rating = 'MCMB' if phase == 'Climb' else ('LIDL' if phase == 'Descent' else 'MCRZ')
            rated_thrust = ac.flightEnvelope.Thrust(
                delta=delta, theta=theta, M=mach, deltaTemp=dtemp, rating=rating)
            thrust = drag if phase == 'Cruise' else rated_thrust
            fuel_args = {'M': mach, 'rating': rating}
            if phase == 'Cruise':
                fuel_args = {'M': mach, 'CT': thrust / (delta * ac.WREF)}
            fuel = ac.flightEnvelope.ff(delta=delta, theta=theta,
                                         deltaTemp=dtemp, **fuel_args)
            esf = ac.esf(flightEvolution=evolution, h=h, M=mach, deltaTemp=dtemp) \
                if bada_phase else 1.0
            rocd = ac.ROCD(T=thrust, D=drag, v=tas, mass=mass, ESF=esf,
                           h=h, deltaTemp=dtemp) if bada_phase else 0.0
        acceleration = (thrust - drag) * (1.0 - esf) / mass if bada_phase else 0.0
        if abs(rocd) > 100.0 or abs(acceleration) > 50.0:
            raise EvaluationError(f'Unbounded TEM output: ROCD={rocd}, acceleration={acceleration}')
        return dict(thrust=float(thrust), rated_thrust=float(rated_thrust),
                    drag=float(drag), fuel_flow=float(fuel), esf=float(esf),
                    rocd=float(rocd), acceleration=float(acceleration))


class ModelStore:
    """Exact/explicit model resolver; no heuristic scientific matching."""

    def __init__(self, family: str, data_path: str, version=None, aliases=None, strict=False):
        self.family = str(family)
        if not version:
            raise ModelUnavailable(f'BADA {self.family} dataset version is required')
        self.version = str(version)
        self.data_path = Path(data_path).expanduser().resolve()
        self.aliases = {str(k).upper(): str(v) for k, v in (aliases or {}).items()}
        self.strict = bool(strict)
        self._cache: dict[str, tuple[Any, Resolution]] = {}
        if not self.data_path.is_dir():
            raise ModelUnavailable(f'BADA {family} data directory does not exist: {self.data_path}')

    def _available(self):
        if self.family == '3':
            return {p.stem.upper(): p.stem for p in self.data_path.glob('*.OPF')}
        return {p.name.upper(): p.name for p in self.data_path.iterdir() if p.is_dir()}

    def _candidate(self, requested, available):
        candidate = available.get(requested)
        method = 'exact'
        # BADA 3 uses fixed-width six-character file codes padded with
        # underscores. Accepting the unpadded code is deterministic
        # canonicalization, not prefix or similarity matching.
        if candidate is None and self.family == '3' and len(requested) <= 6:
            padded = requested.rstrip('_').ljust(6, '_')
            candidate = available.get(padded)
            if candidate is not None:
                method = 'bada3-code'
        if candidate is None and requested in self.aliases:
            candidate = available.get(self.aliases[requested].upper())
            method = 'alias'
        return candidate, method

    def resolve(self, requested: str):
        requested = requested.upper()
        if requested in self._cache:
            return self._cache[requested]
        available = self._available()
        candidate, method = self._candidate(requested, available)
        if candidate is None:
            if self.strict:
                raise ModelUnavailable(f'No exact or approved BADA {self.family} model for {requested}')
            dummy_names = sorted(name for key, name in available.items() if 'DUMMY' in key)
            if self.family == '4':
                preferred = [name for name in dummy_names if name.upper() == 'DUMMY-TWIN']
                dummy_names = preferred + [name for name in dummy_names if name not in preferred]
            if self.family == '3' and not dummy_names:
                dummy_names = [available[k] for k in ('J2M___', 'J2H___') if k in available]
            if not dummy_names:
                raise ModelUnavailable(f'No model or interactive dummy available for {requested}')
            candidate, method = dummy_names[0], 'dummy'
        try:
            module = import_module('pyBADA.bada3' if self.family == '3' else 'pyBADA.bada4')
            cls = getattr(module, 'Bada3Aircraft' if self.family == '3' else 'Bada4Aircraft')
            model = BadaModelAdapter(
                cls(badaVersion=self.version, acName=candidate, filePath=str(self.data_path)),
                self.family)
        except (ImportError, AttributeError, OSError, TypeError, ValueError) as exc:
            raise ModelUnavailable(f'Cannot load BADA {self.family} model {candidate}: {exc}') from exc
        is_dummy = method == 'dummy' or self.data_path.name.upper() == 'DUMMY' \
            or 'DUMMY' in candidate.upper()
        resolution = Resolution(requested, candidate, method, is_dummy)
        self._cache[requested] = model, resolution
        return model, resolution
