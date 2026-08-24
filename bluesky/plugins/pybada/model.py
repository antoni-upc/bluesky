"""Model resolution and typed pyBADA evaluation boundaries."""

from dataclasses import dataclass
from enum import Enum
from importlib import import_module
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

import numpy as np


class ModelUnavailable(RuntimeError):
    pass


class EvaluationError(RuntimeError):
    pass


class BadaConfigurationMode(str, Enum):
    CRUISE = 'CRUISE'
    PYBADA = 'PYBADA'


def parse_configuration_mode(value):
    if isinstance(value, BadaConfigurationMode):
        return value
    try:
        return BadaConfigurationMode(str(value).upper().strip())
    except ValueError as exc:
        raise ValueError('mode must be CRUISE or PYBADA') from exc


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
    idle_thrust: float = np.nan
    maximum_thrust: float = np.nan
    requested_acceleration: float = 0.0
    applied_acceleration: float = 0.0
    thrust_limited: bool = False
    limitation_reason: str = ''
    required_thrust: float = np.nan
    requested_vertical_rate: float = 0.0
    applied_vertical_rate: float = 0.0
    allocation_policy: str = 'HORIZONTAL_ADAPTED'

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
        self._bada4_dlm_limits = None

    def __getattr__(self, name):
        return getattr(self.model, name)

    def _configuration(self, *, phase, h, mass, cas, delta_temp,
                       configuration_mode=BadaConfigurationMode.PYBADA):
        mode = parse_configuration_mode(configuration_mode)
        if mode == BadaConfigurationMode.CRUISE:
            return 'CR'
        return (self.model.flightEnvelope.getConfig(
            phase=phase, h=h, mass=mass, v=cas, deltaTemp=delta_temp) or 'CR')

    @staticmethod
    def _atmosphere(h, tas, temperature):
        atm = import_module('pyBADA.atmosphere')
        dtemp = atm.ISATemperatureDeviation(temperature=temperature, pressureAltitude=h)
        theta = atm.theta(h=h, deltaTemp=dtemp)
        delta = atm.delta(h=h, deltaTemp=dtemp)
        sigma = atm.sigma(theta=theta, delta=delta)
        mach = atm.tas2Mach(v=tas, theta=theta)
        return atm, dtemp, theta, delta, sigma, mach

    def bluesky_energy(self, *, h, tas, mass, temperature, pressure, phase, schedule,
                       configuration_mode=BadaConfigurationMode.PYBADA,
                       requested_acceleration=0.0, requested_vertical_rate=0.0):
        ac = self.model
        atm, dtemp, theta, delta, sigma, mach = self._atmosphere(h, tas, temperature)
        bada_phase = {'Climb': 'cl', 'Descent': 'des'}.get(phase)
        evolution = 'constCAS' if schedule == 'CONSCAS' or h <= 9144.0 else 'constM'
        if self.family == '3':
            cas = atm.tas2Cas(tas=tas, delta=delta, sigma=sigma)
            config = self._configuration(
                phase=phase, h=h, mass=mass, cas=cas, delta_temp=dtemp,
                configuration_mode=configuration_mode)
            lift = ac.flightEnvelope.CL(sigma=sigma, mass=mass, tas=tas)
            drag = ac.flightEnvelope.D(sigma=sigma, tas=tas,
                CD=ac.flightEnvelope.CD(CL=lift, config=config))
            rating = 'MCMB' if phase == 'Climb' else ('LIDL' if phase == 'Descent' else 'MCRZ')
            rated_thrust = ac.Thrust(h=h, deltaTemp=dtemp, rating=rating,
                                     v=tas, config=config)
            idle_thrust = ac.Thrust(h=h, deltaTemp=dtemp, rating='LIDL',
                                    v=tas, config=config)
            maximum_thrust = ac.Thrust(h=h, deltaTemp=dtemp, rating='MCMB',
                                       v=tas, config=config)
            if phase == 'Cruise' and requested_acceleration:
                required_thrust = ac.TAdapted(
                    h=h, deltaTemp=dtemp, ROCD=0.0, mass=mass,
                    v=tas, acc=requested_acceleration, Drag=drag)
            else:
                required_thrust = drag if phase == 'Cruise' else rated_thrust
            airplane = import_module('pyBADA.aircraft').Airplane
            esf = airplane.esf(flightEvolution=evolution, h=h, M=mach, deltaTemp=dtemp) \
                if bada_phase else 1.0
            rocd = ac.ROCD(required_thrust, drag, tas, mass, esf, h, dtemp) \
                if bada_phase else 0.0
        else:
            cas = atm.tas2Cas(tas=tas, delta=delta, sigma=sigma)
            config = self._configuration(
                phase=phase, h=h, mass=mass, cas=cas, delta_temp=dtemp,
                configuration_mode=configuration_mode)
            hlid, gear = ac.flightEnvelope.getAeroConfig(config=config)
            lift = ac.flightEnvelope.CL(delta=delta, mass=mass, M=mach)
            drag = ac.flightEnvelope.D(delta=delta, M=mach,
                CD=ac.flightEnvelope.CD(HLid=hlid, LG=gear, CL=lift, M=mach))
            rating = 'MCMB' if phase == 'Climb' else ('LIDL' if phase == 'Descent' else 'MCRZ')
            rated_thrust = ac.flightEnvelope.Thrust(
                delta=delta, theta=theta, M=mach, deltaTemp=dtemp, rating=rating)
            idle_thrust = ac.flightEnvelope.Thrust(
                delta=delta, theta=theta, M=mach, deltaTemp=dtemp, rating='LIDL')
            maximum_thrust = ac.flightEnvelope.Thrust(
                delta=delta, theta=theta, M=mach, deltaTemp=dtemp, rating='MCMB')
            required_thrust = (drag + mass * requested_acceleration
                               if phase == 'Cruise' and requested_acceleration else
                               (drag if phase == 'Cruise' else rated_thrust))
            esf = ac.esf(flightEvolution=evolution, h=h, M=mach, deltaTemp=dtemp) \
                if bada_phase else 1.0
            rocd = ac.ROCD(T=required_thrust, D=drag, v=tas, mass=mass, ESF=esf,
                           h=h, deltaTemp=dtemp) if bada_phase else 0.0
        lower, upper = sorted((float(idle_thrust), float(maximum_thrust)))
        limited = bool(required_thrust < lower or required_thrust > upper)
        reason = ('BELOW_IDLE_THRUST' if required_thrust < lower else
                  ('ABOVE_MAXIMUM_THRUST' if required_thrust > upper else ''))
        thrust = float(np.clip(required_thrust, lower, upper))
        if self.family == '3':
            fuel = max(ac.ff(h=h, v=tas, T=thrust, config=config,
                             flightPhase=phase,
                             adapted=phase == 'Cruise' and bool(requested_acceleration)),
                       ac.ffMin(h=h))
        else:
            fuel_args = ({'M': mach, 'CT': thrust / (delta * ac.WREF)}
                         if phase == 'Cruise' else {'M': mach, 'rating': rating})
            fuel = ac.flightEnvelope.ff(delta=delta, theta=theta,
                                         deltaTemp=dtemp, **fuel_args)
        acceleration = ((thrust - drag) / mass if phase == 'Cruise' else
                        (thrust - drag) * (1.0 - esf) / mass)
        if abs(rocd) > 100.0 or abs(acceleration) > 50.0:
            raise EvaluationError(f'Unbounded TEM output: ROCD={rocd}, acceleration={acceleration}')
        return dict(thrust=float(thrust), rated_thrust=float(rated_thrust),
                    drag=float(drag), fuel_flow=float(fuel), esf=float(esf),
                    rocd=float(rocd), acceleration=float(acceleration),
                    idle_thrust=float(idle_thrust), maximum_thrust=float(maximum_thrust),
                    requested_acceleration=float(requested_acceleration),
                    applied_acceleration=float(acceleration), thrust_limited=limited,
                    limitation_reason=reason, required_thrust=float(required_thrust),
                    requested_vertical_rate=float(requested_vertical_rate),
                    applied_vertical_rate=float(rocd),
                    allocation_policy=('HORIZONTAL_ADAPTED' if phase == 'Cruise'
                                       else 'BADA_ESF'))

    def bluesky_airdata(self, *, h, tas, temperature):
        """Convert TAS with the same applied-atmosphere convention as TEM."""
        atm, _, theta, delta, sigma, mach = self._atmosphere(h, tas, temperature)
        cas = atm.tas2Cas(tas=tas, delta=delta, sigma=sigma)
        return float(cas), float(mach)

    def bluesky_vertical_envelope(self, *, h, tas, mass, temperature, pressure,
                                  schedule,
                                  configuration_mode=BadaConfigurationMode.PYBADA):
        """Return LIDL descent and MCMB climb ROCD at one operating point."""
        common = dict(h=h, tas=tas, mass=mass, temperature=temperature,
                      pressure=pressure, schedule=schedule,
                      configuration_mode=configuration_mode)
        minimum = float(self.bluesky_energy(phase='Descent', **common)['rocd'])
        maximum = float(self.bluesky_energy(phase='Climb', **common)['rocd'])
        if not np.all(np.isfinite((minimum, maximum))):
            raise EvaluationError('non-finite MCMB/LIDL ROCD bounds')
        if minimum > maximum:
            raise EvaluationError(
                f'contradictory MCMB/LIDL ROCD bounds {minimum}..{maximum}')
        return dict(minimum_rocd=minimum, maximum_rocd=maximum)

    def bluesky_lateral_envelope(self, *, configuration, phase):
        """Return documented BADA lateral/load limits without defaults."""
        if self.family == '3':
            # getConfig is pyBADA's direct aerodynamic-state observation. Its
            # terminal states select the matching documented GPF phase; the
            # broad BlueSky phase remains the source for clean configurations.
            phase_code = {'TO': 'to', 'IC': 'ic', 'AP': 'app', 'LD': 'lnd'}.get(
                str(configuration).upper(),
                {'Climb': 'cl', 'Descent': 'des', 'Cruise': 'cr'}.get(phase, 'cr'))
            try:
                bank = self.model.flightEnvelope.getBankAngle(
                    phase=phase_code, flightUnit='civ', value='max')
            except TypeError as exc:
                # pyBADA 0.1.12's BADA 3 wrapper calls getGPFValue with the
                # obsolete ``flightUnit`` keyword. Read the same documented
                # GPF record narrowly until that upstream compatibility bug is
                # fixed; never substitute a default bank angle.
                if 'flightUnit' not in str(exc):
                    raise
                records = getattr(self.model.flightEnvelope.AC, 'GPFdata', ())
                matches = [item.get('value') for item in records
                           if item.get('name') == 'ang_bank_max'
                           and phase_code in item.get('phase', ())
                           and 'civ' in item.get('flight', ())
                           and any(engine in item.get('engine', ())
                                   for engine in ('JET', 'jet'))]
                if len(matches) != 1:
                    raise EvaluationError(
                        f'BADA 3 GPF has {len(matches)} civilian maximum-bank '
                        f'records for phase {phase_code}') from exc
                bank = matches[0]
            bank = float(bank)
            if not np.isfinite(bank) or bank < 0.0 or bank >= 90.0:
                raise EvaluationError(f'invalid BADA 3 maximum bank angle {bank!r}')
            maximum = 1.0 / np.cos(np.radians(bank))
            return dict(configuration=str(configuration), minimum_load_factor=None,
                        maximum_load_factor=float(maximum),
                        maximum_bank_angle_deg=bank,
                        minimum_limit_name='', maximum_limit_name='derived',
                        high_lift_id=None, landing_gear='')
        if self._bada4_dlm_limits is None:
            base = Path(self.model.filePath)
            candidates = (base / self.model.acName / f'{self.model.acName}.xml',
                          base / f'{self.model.acName}.xml')
            path = next((item for item in candidates if item.is_file()), None)
            if path is None:
                raise EvaluationError(
                    f'BADA 4 DLM XML unavailable for {self.model.acName}')
            dlm = ET.parse(path).getroot().find('.//DLM')
            if dlm is None:
                raise EvaluationError('BADA 4 DLM is missing')
            limits = {}
            for name in ('n1', 'n3', 'nf1', 'nf3'):
                node = dlm.find(name)
                limits[name] = None if node is None else float(node.text)
            self._bada4_dlm_limits = limits
        hlid, gear = self.model.flightEnvelope.getAeroConfig(config=configuration)
        minimum_name, maximum_name = ('n3', 'n1') if float(hlid) == 0.0 else ('nf3', 'nf1')
        minimum = self._bada4_dlm_limits[minimum_name]
        maximum = self._bada4_dlm_limits[maximum_name]
        if minimum is None or maximum is None:
            raise EvaluationError(
                f'BADA 4 DLM lacks {minimum_name}/{maximum_name}')
        if not np.all(np.isfinite((minimum, maximum))) or maximum < 1.0 or minimum > maximum:
            raise EvaluationError('invalid BADA 4 load-factor limits')
        bank = np.degrees(np.arccos(1.0 / maximum))
        return dict(configuration=str(configuration), minimum_load_factor=minimum,
                    maximum_load_factor=maximum,
                    maximum_bank_angle_deg=float(bank), high_lift_id=float(hlid),
                    landing_gear=str(gear), minimum_limit_name=minimum_name,
                    maximum_limit_name=maximum_name)

    def bluesky_envelope(self, *, h, cas, mach, mass, temperature, pressure, phase,
                         configuration_mode=BadaConfigurationMode.PYBADA):
        """Normalize BADA 3/4 longitudinal limits at one operating point."""
        ac = self.model
        tas = mach * np.sqrt(1.4 * 287.05287 * temperature)
        atm, dtemp, theta, delta, sigma, _ = self._atmosphere(h, tas, temperature)
        config = self._configuration(
            phase=phase, h=h, mass=mass, cas=cas, delta_temp=dtemp,
            configuration_mode=configuration_mode)
        if self.family == '3':
            minimum_cas = ac.flightEnvelope.VMin(
                h=h, mass=mass, config=config, deltaTemp=dtemp)
            maximum_cas = ac.flightEnvelope.VMax(h=h, deltaTemp=dtemp)
            maximum_altitude = ac.flightEnvelope.maxAltitude(
                mass=mass, deltaTemp=dtemp)
            maximum_mach = float(ac.MMO)
        else:
            hlid, gear = ac.flightEnvelope.getAeroConfig(config=config)
            minimum_cas = ac.flightEnvelope.VMin(
                config=config, theta=theta, delta=delta, mass=mass)
            maximum_cas = ac.flightEnvelope.VMax(
                h=h, HLid=hlid, LG=gear, delta=delta, theta=theta,
                mass=mass, nz=1.0)
            maximum_altitude = ac.flightEnvelope.maxAltitude(
                HLid=hlid, LG=gear, M=mach, deltaTemp=dtemp,
                mass=mass, nz=1.0)
            maximum_mach = ac.flightEnvelope.maxM(LG=gear)
        def optional_float(value):
            if value is None:
                return None
            value = float(value)
            return value if np.isfinite(value) else None

        minimum_cas = optional_float(minimum_cas)
        maximum_cas = optional_float(maximum_cas)
        maximum_mach = optional_float(maximum_mach)
        maximum_altitude = optional_float(maximum_altitude)
        minimum_mach = (None if minimum_cas is None else optional_float(atm.cas2Mach(
            cas=minimum_cas, theta=theta, delta=delta, sigma=sigma)))
        minimum_tas = (None if minimum_cas is None else optional_float(
            atm.cas2Tas(cas=minimum_cas, delta=delta, sigma=sigma)))
        maximum_tas = (None if maximum_cas is None else optional_float(
            atm.cas2Tas(cas=maximum_cas, delta=delta, sigma=sigma)))
        return dict(configuration=str(config), minimum_cas=minimum_cas,
                    maximum_cas=maximum_cas, minimum_mach=minimum_mach,
                    maximum_mach=maximum_mach,
                    maximum_altitude=maximum_altitude,
                    minimum_tas=minimum_tas, maximum_tas=maximum_tas)


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
