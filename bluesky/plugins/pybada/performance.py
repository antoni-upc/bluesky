"""BlueSky performance implementation for longitudinal/vertical TEM."""

from dataclasses import dataclass, replace

import numpy as np

import bluesky as bs
from bluesky.traffic.performance.perfbase import PerfBase
from bluesky.traffic.dynamics import SpeedStepRequest, SpeedStepResult
try:
    from bluesky.traffic.atmosphere import pressure_altitude
except ImportError:  # Without the NWP atmosphere hook, Traffic uses geometric altitude.
    pressure_altitude = None
from bluesky.tools import aero
from bluesky.tools.aero import g0
from .model import (EnergyResult, EvaluationError, ModelStore, ModelUnavailable,
                    parse_configuration_mode, allocate_speed_priority)
from .envelope import (EnvelopeAction, EnvelopeCheck, EnvelopePolicy, EnvelopeProfile,
                       EnvelopeResult, EnvelopeStatus, FlightBounds, QualityEvent,
                       LateralBounds, VerticalBounds, combine_results, evaluate_flight,
                       evaluate_lateral, evaluate_mass, evaluate_vertical, expand_checks, mass_bounds,
                       parse_policy, quality_events)


bs.settings.set_variable_defaults(
    pybada3_data_path='', pybada4_data_path='', pybada_family='4',
    pybada3_version='', pybada4_version='',
    pybada_strict=False, pybada_aircraft_aliases={}, pybada_speed_schedule='ICAO',
    pybada_envelope_policy='OFF', pybada_envelope_profile='LONGITUDINAL',
    pybada_envelope_checks=[], pybada_configuration_mode='PYBADA')


@dataclass(frozen=True)
class SpeedIntent:
    evolution: str
    target_cas: float = np.nan
    target_mach: float = np.nan
    target_tas: float = np.nan


class PyBadaTEM(PerfBase):
    """One authoritative BADA 3/4 integration with native lateral guidance."""

    requires_synced_direct_state = True
    preserves_direct_mach = True

    def __init__(self):
        super().__init__()
        self.family = str(bs.settings.pybada_family)
        self.version = ''
        self.schedule = str(bs.settings.pybada_speed_schedule).upper()
        self.strict = bool(bs.settings.pybada_strict)
        self.store = None
        self.models = []
        self.resolutions = []
        self.envelope_checks = []
        self.envelope_failed_checks = []
        self.envelope_mass_failed_checks = []
        self.envelope_state_failed_checks = []
        self.envelope_guidance_failed_checks = []
        with self.settrafarrays():
            self.dyn_mode = np.array([], dtype=int)
            self.bada_configuration_mode = np.array([], dtype='U8')
            self.rated_thrust = np.array([])
            self.required_thrust = np.array([])
            self.idle_thrust = np.array([])
            self.maximum_thrust = np.array([])
            self.target_tas = np.array([])
            self.requested_acceleration = np.array([])
            self.applied_acceleration = np.array([])
            self.thrust_limited = np.array([], dtype=bool)
            self.thrust_limitation_reason = np.array([], dtype='U32')
            self.speed_capture = np.array([], dtype=bool)
            self.requested_vertical_rate = np.array([])
            self.applied_vertical_rate = np.array([])
            self.evaluation_tas = np.array([])
            self.evaluation_alt = np.array([])
            self.evaluation_mass = np.array([])
            self.evaluation_temperature = np.array([])
            self.evaluation_pressure_alt = np.array([])
            self.evaluation_timestep = np.array([])
            self.evaluation_speed_evolution = np.array([], dtype='U8')
            self.evaluation_speed_target_cas = np.array([])
            self.evaluation_speed_target_mach = np.array([])
            self.evaluation_speed_target_tas = np.array([])
            self.model_rocd = np.array([])
            self.energy_share_factor = np.array([])
            self.energy_allocation_policy = np.array([], dtype='U24')
            self.propulsion_bank_angle = np.array([])
            self.propulsion_load_factor = np.array([])
            self.mass_override = np.array([], dtype=bool)
            self.invalid = np.array([], dtype=bool)
            self.failure_count = np.array([], dtype=int)
            self.envelope_policy = np.array([], dtype='U8')
            self.envelope_profile = np.array([], dtype='U12')
            self.envelope_status = np.array([], dtype='U10')
            self.envelope_last_action = np.array([], dtype='U8')
            self.envelope_last_reason = np.array([], dtype='U80')
            self.envelope_active_reason = np.array([], dtype='U80')
            self.envelope_mass_reason = np.array([], dtype='U80')
            self.envelope_state_reason = np.array([], dtype='U80')
            self.envelope_guidance_reason = np.array([], dtype='U80')
            self.envelope_attempt_reason = np.array([], dtype='U80')
            self.envelope_dynamics_reason = np.array([], dtype='U80')
            self.envelope_guidance_infeasible = np.array([], dtype=bool)
            self.envelope_event_count = np.array([], dtype=int)
            self.envelope_violation_count = np.array([], dtype=int)

    def activate(self, family=None):
        family = str(family or self.family).replace('BADA', '')
        if family not in ('3', '4'):
            raise ValueError('PERFMODEL accepts BADA3 or BADA4')
        data_path = bs.settings.pybada3_data_path if family == '3' else bs.settings.pybada4_data_path
        version = bs.settings.pybada3_version if family == '3' else bs.settings.pybada4_version
        if not data_path or not version:
            raise ModelUnavailable(
                f'Configure both pybada{family}_data_path and pybada{family}_version')
        candidate_store = ModelStore(family, data_path, version,
                                     bs.settings.pybada_aircraft_aliases, self.strict)
        candidate_models = []
        candidate_resolutions = []
        for actype in bs.traf.type:
            model, resolution = candidate_store.resolve(actype)
            candidate_models.append(model)
            candidate_resolutions.append(resolution)
        evaluations = []
        if (hasattr(self, 'envelope_policy') and
                len(self.envelope_checks) == len(candidate_models)):
            for idx, model in enumerate(candidate_models):
                policy = parse_policy(self.envelope_policy[idx])
                evaluation, _, _, _ = self.evaluate_envelope(idx, model=model)
                if policy != EnvelopePolicy.OFF and evaluation.status == EnvelopeStatus.UNKNOWN:
                    raise ModelUnavailable(f'{bs.traf.id[idx]} envelope unknown: {evaluation.reason}')
                if policy == EnvelopePolicy.ENFORCE and evaluation.status == EnvelopeStatus.INFEASIBLE:
                    raise ModelUnavailable(f'{bs.traf.id[idx]} envelope infeasible: {evaluation.reason}')
                evaluations.append((policy, evaluation))
        # Commit only after all current aircraft resolve. A failed family
        # switch leaves the complete prior implementation state untouched.
        self.store = candidate_store
        self.family = family
        self.version = candidate_store.version
        self.models[:] = candidate_models
        self.resolutions[:] = candidate_resolutions
        for idx, (policy, evaluation) in enumerate(evaluations):
            if policy != EnvelopePolicy.OFF:
                action = EnvelopeAction.ABORTED if (policy == EnvelopePolicy.ABORT and
                    evaluation.status == EnvelopeStatus.INFEASIBLE) else EnvelopeAction.ACCEPTED
                self._set_result(idx, evaluation, policy, action, self.mass[idx], self.mass[idx])
                if action == EnvelopeAction.ABORTED:
                    bs.sim.hold()

    def create(self, n):
        super().create(n)
        self.evaluation_speed_evolution[-n:] = ''
        self.evaluation_speed_target_cas[-n:] = np.nan
        self.evaluation_speed_target_mach[-n:] = np.nan
        self.evaluation_speed_target_tas[-n:] = np.nan
        self.dyn_mode[-n:] = 1
        self.bada_configuration_mode[-n:] = parse_configuration_mode(
            bs.settings.pybada_configuration_mode).value
        if self.store is None:
            self.activate()
        for actype in bs.traf.type[-n:]:
            model, resolution = self.store.resolve(actype)
            self.models.append(model)
            self.resolutions.append(resolution)
            profile = EnvelopeProfile(str(bs.settings.pybada_envelope_profile).upper())
            from .envelope import parse_checks
            explicit = parse_checks(bs.settings.pybada_envelope_checks)
            self.envelope_checks.append(expand_checks(profile, explicit))
            self.envelope_failed_checks.append(())
            self.envelope_mass_failed_checks.append(())
            self.envelope_state_failed_checks.append(())
            self.envelope_guidance_failed_checks.append(())
        self.envelope_policy[-n:] = parse_policy(bs.settings.pybada_envelope_policy).value
        self.envelope_profile[-n:] = str(bs.settings.pybada_envelope_profile).upper()
        self.envelope_status[-n:] = EnvelopeStatus.VALID.value
        self.envelope_last_action[-n:] = EnvelopeAction.NONE.value
        self.envelope_last_reason[-n:] = ''
        self.envelope_active_reason[-n:] = ''
        self.envelope_mass_reason[-n:] = ''
        self.envelope_state_reason[-n:] = ''
        self.envelope_guidance_reason[-n:] = ''
        self.envelope_attempt_reason[-n:] = ''
        self.envelope_dynamics_reason[-n:] = ''
        self.envelope_guidance_infeasible[-n:] = False
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
            del self.envelope_checks[i]
            del self.envelope_failed_checks[i]
            del self.envelope_mass_failed_checks[i]
            del self.envelope_state_failed_checks[i]
            del self.envelope_guidance_failed_checks[i]
        super().delete(idx)

    def reset(self):
        self.models.clear()
        self.resolutions.clear()
        self.envelope_checks.clear()
        self.envelope_failed_checks.clear()
        self.envelope_mass_failed_checks.clear()
        self.envelope_state_failed_checks.clear()
        self.envelope_guidance_failed_checks.clear()
        super().reset()

    def bounds(self, idx):
        return mass_bounds(self.models[idx])

    def _phase(self, idx):
        target = bs.traf.aporasas.alt[idx]
        # Keep the vertical energy policy active through target capture. A
        # one-metre phase deadband leaves TEM permanently short of the selected
        # altitude because Cruise correctly returns zero ROCD.
        return ('Climb' if target > bs.traf.alt[idx] + 0.01 else
                ('Descent' if target < bs.traf.alt[idx] - 0.01 else 'Cruise'))

    def _configuration_mode(self, idx, override=None):
        if override is not None:
            return parse_configuration_mode(override).value
        values = getattr(self, 'bada_configuration_mode', None)
        return ('PYBADA' if values is None or idx >= len(values)
                else parse_configuration_mode(values[idx]).value)

    def _speed_evolution(self, idx):
        return self._capture_speed_intent(idx).evolution

    def _capture_speed_intent(self, idx):
        """Snapshot the evaluated law and original target representation."""
        active = getattr(getattr(bs.traf, 'cr', None), 'tasactive', None)
        if self.schedule != 'CONSCAS' and active is not None and active[idx]:
            target = float(bs.traf.aporasas.tas[idx])
            if not np.isfinite(target) or target <= 0:
                raise EvaluationError('Resolved TAS target must be finite and positive')
            return SpeedIntent('constTAS', target_tas=target)
        selected = float(bs.traf.selspd[idx])
        if not np.isfinite(selected) or selected <= 0:
            raise EvaluationError('Selected CAS/Mach speed must be finite and positive')
        if 0.1 < selected < aero.casmach_thr:
            return SpeedIntent('constCAS' if self.schedule == 'CONSCAS' else 'constM',
                               target_mach=selected)
        return SpeedIntent('constCAS', target_cas=selected)

    def _record_speed_intent(self, idx, intent):
        if hasattr(self, 'evaluation_speed_evolution'):
            self.evaluation_speed_evolution[idx] = intent.evolution
            self.evaluation_speed_target_cas[idx] = intent.target_cas
            self.evaluation_speed_target_mach[idx] = intent.target_mach
            self.evaluation_speed_target_tas[idx] = intent.target_tas

    def _clear_speed_intent(self, idx):
        if hasattr(self, 'evaluation_speed_evolution'):
            self.evaluation_speed_evolution[idx] = ''
            self.evaluation_speed_target_cas[idx] = np.nan
            self.evaluation_speed_target_mach[idx] = np.nan
            self.evaluation_speed_target_tas[idx] = np.nan

    def _evaluation_intent(self, idx, override=None):
        if override is not None:
            return override
        active = getattr(self, '_active_speed_intent', None)
        if active is not None and active[0] == idx:
            return active[1]
        return self._capture_speed_intent(idx)

    @staticmethod
    def _call_configuration_aware(function, configuration_mode, **kwargs):
        try:
            return function(configuration_mode=configuration_mode, **kwargs)
        except TypeError as exc:
            if "unexpected keyword argument 'configuration_mode'" not in str(exc):
                raise
            return function(**kwargs)

    def flight_bounds(self, idx, *, mass=None, cas=None, mach=None, model=None,
                      configuration_mode=None):
        model = model or self.models[idx]
        try:
            values = self._call_configuration_aware(model.bluesky_envelope,
                self._configuration_mode(idx, configuration_mode),
                h=float(bs.traf.pressure_alt[idx]),
                cas=float(bs.traf.cas[idx] if cas is None else cas),
                mach=float(bs.traf.M[idx] if mach is None else mach),
                mass=float(self.mass[idx] if mass is None else mass),
                temperature=float(bs.traf.Temp[idx]), pressure=float(bs.traf.p[idx]),
                phase=self._phase(idx))
            return FlightBounds(**values)
        except Exception as exc:
            return FlightBounds('', None, None, None, None, None,
                                reason=f'envelope evaluation failed: {exc}')

    @staticmethod
    def _pressure_altitude_at(idx, geometric_altitude):
        """Use the same atmosphere source as Traffic at a geometric target."""
        altitude = float(geometric_altitude)
        if altitude == float(bs.traf.alt[idx]):
            return float(bs.traf.pressure_alt[idx])
        if not np.isfinite(altitude):
            return np.nan
        if pressure_altitude is None:
            return altitude
        pressure = float(aero.vatmos(np.array([altitude]))[0][0])
        get_atmosphere = getattr(getattr(bs.traf, 'wind', None), 'get_atmosphere', None)
        if get_atmosphere is not None:
            sample = get_atmosphere(
                np.array([float(bs.traf.lat[idx])]),
                np.array([float(bs.traf.lon[idx])]),
                np.array([altitude]), getattr(bs.sim, 'utc', None))
            if sample is not None:
                valid = np.asarray(sample.valid).reshape(-1)
                values = [np.asarray(field, dtype=float).reshape(-1)
                          for field in (sample.temperature, sample.pressure, sample.density)]
                if (valid.size != 1 or any(value.size != 1 for value in values)
                        or not bool(valid[0]) or not all(np.isfinite(value[0]) and value[0] > 0
                                                         for value in values)):
                    return np.nan
                pressure = float(values[1][0])
        return float(pressure_altitude(np.array([pressure]))[0])

    def _geometric_ceiling(self, idx, ceiling, requested_altitude):
        """Find a geometric target at the pressure-altitude ceiling."""
        upper = float(requested_altitude)
        lower = min(float(bs.traf.alt[idx]), upper)
        lower_pressure_alt = self._pressure_altitude_at(idx, lower)
        if not np.isfinite(lower_pressure_alt):
            raise RuntimeError('ceiling conversion requires valid atmosphere at the lower altitude')
        if lower_pressure_alt > ceiling:
            cube = getattr(getattr(bs.traf, 'wind', None), 'cube', None)
            lower = float(cube.altitude[0]) if cube is not None else -1000.0
            lower_pressure_alt = self._pressure_altitude_at(idx, lower)
            if not np.isfinite(lower_pressure_alt) or lower_pressure_alt > ceiling:
                raise RuntimeError('ceiling conversion has no valid altitude below the ceiling')
        for _ in range(32):
            midpoint = (lower + upper) / 2.0
            midpoint_pressure_alt = self._pressure_altitude_at(idx, midpoint)
            if not np.isfinite(midpoint_pressure_alt):
                raise RuntimeError('ceiling conversion requires valid atmosphere')
            if midpoint_pressure_alt <= ceiling:
                lower = midpoint
            else:
                upper = midpoint
        return lower

    def vertical_bounds(self, idx, *, mass=None, tas=None, model=None,
                        configuration_mode=None, speed_intent=None):
        model = model or self.models[idx]
        try:
            values = self._call_configuration_aware(model.bluesky_vertical_envelope,
                self._configuration_mode(idx, configuration_mode),
                h=float(bs.traf.pressure_alt[idx]),
                tas=float(bs.traf.tas[idx] if tas is None else tas),
                mass=float(self.mass[idx] if mass is None else mass),
                temperature=float(bs.traf.Temp[idx]), pressure=float(bs.traf.p[idx]),
                schedule=self.schedule, speed_evolution=(
                    self._evaluation_intent(idx, speed_intent)).evolution)
            return VerticalBounds(**values)
        except Exception as exc:
            return VerticalBounds(None, None,
                                  reason=f'vertical envelope evaluation failed: {exc}')

    def effective_bank_angle(self, idx):
        if not bool(bs.traf.swhdgsel[idx]):
            return 0.0
        selected = float(bs.traf.ap.turnphi[idx])
        bank = selected if selected > float(bs.traf.eps[idx]) ** 2 else \
            float(bs.traf.ap.bankdef[idx])
        return float(np.degrees(bank))

    def propulsion_turn_state(self, idx):
        """Return the finite coordinated-turn state requested this tick."""
        selected = float(bs.traf.ap.turnphi[idx])
        default = float(bs.traf.ap.bankdef[idx])
        bank = selected if selected > float(bs.traf.eps[idx]) ** 2 else default
        delta = (float(bs.traf.aporasas.hdg[idx]) - float(bs.traf.hdg[idx]) + 180.0) % 360.0 - 180.0
        bank_deg = 0.0 if abs(delta) <= float(bs.traf.eps[idx]) else float(np.degrees(bank))
        if not np.isfinite(bank_deg) or abs(bank_deg) >= 90.0:
            raise EvaluationError('current-tick bank angle must be finite and below 90 degrees')
        load = 1.0 / np.cos(np.radians(abs(bank_deg)))
        if not np.isfinite(load) or load < 1.0:
            raise EvaluationError('current-tick load factor is not physical')
        return bank_deg, float(load)

    def lateral_bounds(self, idx, *, model=None, configuration=None,
                       configuration_mode=None):
        model = model or self.models[idx]
        try:
            if configuration is None:
                configuration = self.flight_bounds(
                    idx, model=model,
                    configuration_mode=configuration_mode).configuration
            values = model.bluesky_lateral_envelope(
                configuration=configuration, phase=self._phase(idx))
            return LateralBounds(**values)
        except Exception as exc:
            return LateralBounds('', None, None, None,
                                 reason=f'lateral envelope evaluation failed: {exc}')

    def evaluate_envelope(self, idx, *, mass=None, cas=None, mach=None,
                          altitude=None, vertical_rate=None, bank_angle=None,
                          checks=None, model=None, configuration_mode=None):
        checks = self.envelope_checks[idx] if checks is None else tuple(checks)
        candidate_mass = self.mass[idx] if mass is None else mass
        mbounds = mass_bounds(model or self.models[idx])
        flight_checks = {EnvelopeCheck.LOW_SPEED, EnvelopeCheck.HIGH_SPEED,
                         EnvelopeCheck.MACH_MIN, EnvelopeCheck.MACH_MAX,
                         EnvelopeCheck.ALTITUDE_MAX}
        fbounds = (self.flight_bounds(idx, mass=candidate_mass, cas=cas,
                                     mach=mach, model=model,
                                     configuration_mode=configuration_mode)
                   if set(checks).intersection(flight_checks)
                   else FlightBounds('', None, None, None, None, None))
        vertical_checks = {EnvelopeCheck.ROC_MAX, EnvelopeCheck.ROD_MAX}
        vbounds = (self.vertical_bounds(
                       idx, mass=candidate_mass, model=model,
                       configuration_mode=configuration_mode)
                   if set(checks).intersection(vertical_checks)
                   else VerticalBounds(None, None))
        lateral_checks = {EnvelopeCheck.BANK_ANGLE, EnvelopeCheck.LOAD_FACTOR}
        lbounds = (self.lateral_bounds(idx, model=model,
                                      configuration=fbounds.configuration or None,
                                      configuration_mode=configuration_mode)
                   if set(checks).intersection(lateral_checks)
                   else LateralBounds('', None, None, None))
        bank = ((self.effective_bank_angle(idx) if bank_angle is None else float(bank_angle))
                if set(checks).intersection(lateral_checks) else 0.0)
        load = 1.0 / np.cos(np.radians(abs(bank))) if abs(bank) < 90.0 else np.inf
        flight_altitude = (float(bs.traf.pressure_alt[idx]) if altitude is None or
                           EnvelopeCheck.ALTITUDE_MAX not in checks else
                           self._pressure_altitude_at(idx, altitude))
        return combine_results(
            evaluate_mass(candidate_mass, mbounds, checks),
            evaluate_flight(
                bs.traf.cas[idx] if cas is None else cas,
                bs.traf.M[idx] if mach is None else mach,
                flight_altitude,
                fbounds, checks),
            evaluate_vertical((getattr(bs.traf, 'vs', np.zeros(len(bs.traf.id)))[idx]
                               if vertical_rate is None else vertical_rate),
                              vbounds, checks),
            evaluate_lateral(bank, load, lbounds, checks)), fbounds, vbounds, lbounds

    def _emit_event(self, idx, reason, action, requested=None, applied=None):
        event = QualityEvent(
            aircraft=bs.traf.id[idx], component='PYBADATEM', reason=reason,
            policy=str(self.envelope_policy[idx]), action=action.value,
            continuation='STOP' if action == EnvelopeAction.ABORTED else 'CONTINUE',
            requested=requested, applied=applied,
            sim_time_s=getattr(getattr(bs, 'sim', None), 'simt', None))
        self.envelope_event_count[idx] += 1
        if reason:
            self.envelope_violation_count[idx] += 1
        self.envelope_last_action[idx] = action.value
        self.envelope_last_reason[idx] = reason
        values = (f'requested={self._format_event_value(reason, requested)} '
                  f'applied={self._format_event_value(reason, applied)}')
        message = (f'QUALITY aircraft={event.aircraft} component={event.component} '
                   f'reason={reason} policy={event.policy} action={event.action} '
                   f'{values} continuation={event.continuation}')
        print(message)
        # ``print`` is retained for detached/headless evidence; stack.echo
        # publishes the same event to the interactive BlueSky console.
        from bluesky import stack
        stack.echo(message)
        quality_events.emit(event)
        return event

    @staticmethod
    def _format_event_value(reason, value):
        if value is None:
            return '{none}'
        reasons = set(str(reason).split(','))
        if not isinstance(value, dict):
            key = ('mass_kg' if reasons.intersection(
                   {EnvelopeCheck.MASS_MIN.value, EnvelopeCheck.MASS_MAX.value})
                   else 'value')
            value = {key: value}
        relevant = []
        if reasons.intersection({EnvelopeCheck.LOW_SPEED.value,
                                 EnvelopeCheck.HIGH_SPEED.value}):
            relevant.extend(('tas_m_s', 'cas_m_s'))
        if reasons.intersection({EnvelopeCheck.MACH_MIN.value,
                                 EnvelopeCheck.MACH_MAX.value}):
            relevant.append('mach')
        if EnvelopeCheck.ALTITUDE_MAX.value in reasons:
            relevant.append('altitude_m')
        if reasons.intersection({EnvelopeCheck.ROC_MAX.value,
                                 EnvelopeCheck.ROD_MAX.value}):
            relevant.append('vertical_rate_m_s')
        if reasons.intersection({EnvelopeCheck.MASS_MIN.value,
                                 EnvelopeCheck.MASS_MAX.value}):
            relevant.append('mass_kg')
        if EnvelopeCheck.BANK_ANGLE.value in reasons:
            relevant.append('bank_angle_deg')
        if EnvelopeCheck.LOAD_FACTOR.value in reasons:
            relevant.append('load_factor')
        keys = [key for key in dict.fromkeys(relevant) if key in value]
        if not keys:
            keys = list(value)
        parts = []
        for key in keys:
            item = value[key]
            if key == 'vertical_rate_m_s':
                try:
                    rate = float(item)
                    direction = ('CLIMB' if rate > 0.0 else
                                 ('DESCENT' if rate < 0.0 else 'LEVEL'))
                    parts.append(f'direction={direction}')
                    parts.append(f'vertical_rate_magnitude_m_s={abs(rate):.2f}')
                    continue
                except (TypeError, ValueError):
                    pass
            try:
                digits = 3 if key in ('mach', 'load_factor') else 2
                text = f'{float(item):.{digits}f}' if np.isfinite(item) else str(item)
            except (TypeError, ValueError):
                text = str(item)
            parts.append(f'{key}={text}')
        return '{' + ','.join(parts) + '}'

    def _refresh_envelope_status(self, idx):
        failed = list(self.envelope_mass_failed_checks[idx])
        failed.extend(self.envelope_state_failed_checks[idx])
        if self.envelope_guidance_infeasible[idx]:
            failed.extend(self.envelope_guidance_failed_checks[idx])
        self.envelope_failed_checks[idx] = tuple(dict.fromkeys(failed))
        self.envelope_status[idx] = (EnvelopeStatus.INFEASIBLE.value
                                     if failed else EnvelopeStatus.VALID.value)
        reasons = [self.envelope_mass_reason[idx], self.envelope_state_reason[idx]]
        if self.envelope_guidance_infeasible[idx]:
            reasons.append(self.envelope_guidance_reason[idx])
        self.envelope_active_reason[idx] = ','.join(filter(None, reasons))

    def _set_result(self, idx, result, policy, action, requested=None, applied=None,
                    source='state', contributes=True, publish=True):
        if source == 'mass':
            reason_array = self.envelope_mass_reason
            failed_list = self.envelope_mass_failed_checks
        elif source == 'state':
            reason_array = self.envelope_state_reason
            failed_list = self.envelope_state_failed_checks
        elif source == 'guidance':
            reason_array = self.envelope_guidance_reason
            failed_list = self.envelope_guidance_failed_checks
        elif source == 'attempt':
            reason_array = self.envelope_attempt_reason
            failed_list = None
        else:
            reason_array = self.envelope_dynamics_reason
            failed_list = None
        previous_reason = reason_array[idx]
        other_active_reasons = [
            self.envelope_mass_reason[idx], self.envelope_state_reason[idx],
            self.envelope_dynamics_reason[idx]]
        if self.envelope_guidance_infeasible[idx]:
            other_active_reasons.append(self.envelope_guidance_reason[idx])
        source_reason = {'mass': self.envelope_mass_reason,
                         'state': self.envelope_state_reason,
                         'guidance': self.envelope_guidance_reason,
                         'dynamics': self.envelope_dynamics_reason}.get(source)
        if source_reason is not None:
            try:
                other_active_reasons.remove(source_reason[idx])
            except ValueError:
                pass
        reason_array[idx] = '' if result.status == EnvelopeStatus.VALID else result.reason
        if failed_list is not None:
            failed_list[idx] = (() if result.status == EnvelopeStatus.VALID
                                else result.failed_checks)
        if source == 'guidance':
            self.envelope_guidance_infeasible[idx] = (
                contributes and result.status == EnvelopeStatus.INFEASIBLE)
        self._refresh_envelope_status(idx)
        # A terminal action must be published even when its reason was already
        # reported under a policy that allowed the aircraft to continue.
        abort_transition = (action == EnvelopeAction.ABORTED and
                            self.envelope_last_action[idx] != EnvelopeAction.ABORTED.value)
        if (publish and result.status != EnvelopeStatus.VALID
                and (previous_reason != result.reason or abort_transition)
                and (source == 'attempt' or result.reason not in other_active_reasons
                     or (source == 'guidance' and action == EnvelopeAction.LIMITED)
                     or action == EnvelopeAction.ABORTED)):
            self._emit_event(idx, result.reason, action, requested, applied)

    def _clear_envelope_sources(self, idx):
        self.envelope_state_reason[idx] = ''
        self.envelope_mass_reason[idx] = ''
        self.envelope_guidance_reason[idx] = ''
        self.envelope_attempt_reason[idx] = ''
        self.envelope_dynamics_reason[idx] = ''
        self.envelope_state_failed_checks[idx] = ()
        self.envelope_mass_failed_checks[idx] = ()
        self.envelope_guidance_failed_checks[idx] = ()
        self.envelope_guidance_infeasible[idx] = False
        self._refresh_envelope_status(idx)

    def _current_envelope_values(self, idx):
        bank = self.effective_bank_angle(idx)
        load = (1.0 / np.cos(np.radians(abs(bank)))
                if np.isfinite(bank) and abs(bank) < 90.0 else np.inf)
        return {'mass_kg': float(self.mass[idx]),
                'tas_m_s': float(bs.traf.tas[idx]),
                'cas_m_s': float(bs.traf.cas[idx]),
                'mach': float(bs.traf.M[idx]),
                'altitude_m': float(bs.traf.alt[idx]),
                'vertical_rate_m_s': float(bs.traf.vs[idx]),
                'bank_angle_deg': float(bank),
                'load_factor': float(load)}

    def configure_envelope(self, idx, *, policy=None, profile=None, checks=None):
        old = (self.envelope_policy[idx], self.envelope_profile[idx], self.envelope_checks[idx])
        new_policy = parse_policy(policy or old[0])
        from .envelope import parse_profile
        new_profile = parse_profile(profile or old[1])
        new_checks = tuple(checks) if checks is not None else (
            self.envelope_checks[idx] if profile is None else expand_checks(new_profile))
        result = EnvelopeStatus.VALID
        evaluation, _, _, _ = self.evaluate_envelope(idx, checks=new_checks)
        if new_policy != EnvelopePolicy.OFF:
            result = evaluation.status
            if result == EnvelopeStatus.UNKNOWN:
                return False, evaluation.reason
            if result == EnvelopeStatus.INFEASIBLE and new_policy == EnvelopePolicy.ENFORCE:
                return False, evaluation.reason
        self.envelope_policy[idx] = new_policy.value
        self.envelope_profile[idx] = new_profile.value
        self.envelope_checks[idx] = new_checks
        if new_policy == EnvelopePolicy.OFF:
            self._clear_envelope_sources(idx)
        else:
            action = EnvelopeAction.ABORTED if new_policy == EnvelopePolicy.ABORT else EnvelopeAction.ACCEPTED
            current = self._current_envelope_values(idx)
            self._set_result(idx, evaluation, new_policy, action, current, current)
            if evaluation.status == EnvelopeStatus.INFEASIBLE and new_policy == EnvelopePolicy.ABORT:
                bs.sim.hold()
        return True, ''

    def configure_bada_configuration(self, idx, mode):
        """Change one aircraft's BADA configuration source transactionally."""
        new_mode = parse_configuration_mode(mode)
        try:
            self._evaluate(idx, configuration_mode=new_mode.value)
            policy = parse_policy(self.envelope_policy[idx])
            evaluation = None
            if policy != EnvelopePolicy.OFF:
                evaluation, _, _, _ = self.evaluate_envelope(
                    idx, configuration_mode=new_mode.value)
                if evaluation.status == EnvelopeStatus.UNKNOWN:
                    return False, evaluation.reason
                if (policy == EnvelopePolicy.ENFORCE and
                        evaluation.status == EnvelopeStatus.INFEASIBLE):
                    return False, evaluation.reason
        except (EvaluationError, ModelUnavailable) as exc:
            return False, str(exc)
        self.bada_configuration_mode[idx] = new_mode.value
        if evaluation is not None:
            action = (EnvelopeAction.ABORTED
                      if policy == EnvelopePolicy.ABORT and
                      evaluation.status == EnvelopeStatus.INFEASIBLE
                      else EnvelopeAction.ACCEPTED)
            self._set_result(idx, evaluation, policy, action,
                             {'configuration_mode': new_mode.value},
                             {'configuration_mode': new_mode.value})
            if action == EnvelopeAction.ABORTED:
                bs.sim.hold()
        return True, ''

    def assign_mass(self, idx, value, override=True, runtime=False):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return False, 'mass must be numeric'
        fundamental = evaluate_mass(value, self.bounds(idx), ())
        if fundamental.status == EnvelopeStatus.UNKNOWN:
            return False, fundamental.reason
        policy = parse_policy(self.envelope_policy[idx])
        if policy == EnvelopePolicy.OFF:
            self.mass[idx], self.mass_override[idx] = value, override
            self._clear_envelope_sources(idx)
            return True, ''
        runtime_checks = (tuple(check for check in self.envelope_checks[idx]
                                if check in {EnvelopeCheck.MASS_MIN,
                                             EnvelopeCheck.MASS_MAX})
                          if runtime else None)
        result, _, _, _ = self.evaluate_envelope(
            idx, mass=value, checks=runtime_checks)
        if result.status == EnvelopeStatus.UNKNOWN:
            return False, result.reason
        if result.status == EnvelopeStatus.INFEASIBLE and policy == EnvelopePolicy.ENFORCE:
            self._set_result(idx, result, policy, EnvelopeAction.REJECTED,
                             value, self.mass[idx], source='attempt', contributes=False)
            return False, result.reason
        self.mass[idx], self.mass_override[idx] = value, override
        result_source = 'mass' if runtime else 'state'
        self._set_result(idx, EnvelopeResult(EnvelopeStatus.VALID), policy,
                         EnvelopeAction.NONE, source='attempt', contributes=False)
        action = EnvelopeAction.ABORTED if policy == EnvelopePolicy.ABORT and result.status != EnvelopeStatus.VALID else EnvelopeAction.ACCEPTED
        self._set_result(idx, result, policy, action, value, value,
                         source=result_source)
        if action == EnvelopeAction.ABORTED:
            bs.sim.hold()
        return True, ''

    def assess_direct_state(self, idx, previous):
        """Assess a synchronized provisional MOVE state transactionally."""
        policy = parse_policy(self.envelope_policy[idx])
        result, bounds, vertical, lateral = self.evaluate_envelope(
            idx, checks=() if policy == EnvelopePolicy.OFF else None)
        if result.status == EnvelopeStatus.UNKNOWN:
            return False, f'reason={result.reason}; prior state preserved'
        if policy == EnvelopePolicy.OFF:
            self.envelope_status[idx] = EnvelopeStatus.VALID.value
            return True, ''
        requested = {'cas_m_s': float(bs.traf.cas[idx]), 'mach': float(bs.traf.M[idx]),
                     'altitude_m': float(bs.traf.alt[idx]),
                     'vertical_rate_m_s': float(bs.traf.vs[idx])}
        applied = (None if previous is None else
                   {'cas_m_s': float(previous['cas']), 'mach': float(previous['M']),
                    'altitude_m': float(previous['alt']),
                    'vertical_rate_m_s': float(previous['vs'])})
        if result.status == EnvelopeStatus.INFEASIBLE and policy == EnvelopePolicy.ENFORCE:
            self._set_result(idx, result, policy, EnvelopeAction.REJECTED,
                             requested, applied, source='attempt', contributes=False)
            return False, (f'policy=ENFORCE, reason={result.reason}, '
                           f'CAS bounds={self._bound_text(bounds.minimum_cas)}..'
                           f'{self._bound_text(bounds.maximum_cas)} m/s, '
                           f'Mach bounds={self._bound_text(bounds.minimum_mach)}..'
                           f'{self._bound_text(bounds.maximum_mach)}, '
                           f'pressure-altitude max={self._bound_text(bounds.maximum_altitude)} m, '
                           f'vertical bounds=ROC_MAX '
                           f'{self._bound_text(vertical.maximum_rocd)} m/s, ROD_MAX '
                           f'{self._bound_text_abs(vertical.minimum_rocd)} m/s; '
                           f'lateral bounds=BANK_MAX '
                           f'{self._bound_text(lateral.maximum_bank_angle_deg)} deg, '
                           f'LOAD={self._bound_text(lateral.minimum_load_factor)}..'
                           f'{self._bound_text(lateral.maximum_load_factor)}; '
                           'prior state preserved')
        action = (EnvelopeAction.ABORTED if policy == EnvelopePolicy.ABORT and
                  result.status == EnvelopeStatus.INFEASIBLE else EnvelopeAction.ACCEPTED)
        self._set_result(idx, EnvelopeResult(EnvelopeStatus.VALID), policy,
                         EnvelopeAction.NONE, source='attempt', contributes=False)
        self._set_result(idx, result, policy, action, requested, requested)
        if action == EnvelopeAction.ABORTED:
            bs.sim.hold()
        return True, ''

    def _evaluate(self, idx, configuration_mode=None, speed_intent=None):
        """Evaluate the pyBADA API through one observable failure boundary."""
        ac = self.models[idx]
        h, tas, mass = bs.traf.pressure_alt[idx], bs.traf.tas[idx], self.mass[idx]
        phase = self._phase(idx)
        speed_request = getattr(bs.traf, 'speed_request', None)
        requested_acceleration = (0.0 if speed_request is None else
                                  float(speed_request.requested_acceleration[idx]))
        delta_alt = bs.traf.aporasas.alt[idx] - bs.traf.alt[idx]
        selected_vs = getattr(bs.traf.aporasas, 'vs', np.zeros(bs.traf.ntraf))[idx]
        requested_vertical_rate = (0.0 if abs(delta_alt) <= 1.0 else
                                   float(np.sign(delta_alt) * abs(selected_vs)))
        try:
            propulsion_bank_angle, load_factor = self.propulsion_turn_state(idx)
            # Adapter-friendly hook used by dependency-free fakes and future
            # pyBADA-version-specific adapters.
            if hasattr(ac, 'bluesky_energy'):
                values = self._call_configuration_aware(ac.bluesky_energy,
                    self._configuration_mode(idx, configuration_mode),
                    h=h, tas=tas, mass=mass,
                    temperature=bs.traf.Temp[idx], pressure=bs.traf.p[idx], phase=phase,
                    schedule=self.schedule, speed_evolution=(
                        self._evaluation_intent(idx, speed_intent)).evolution,
                    requested_acceleration=requested_acceleration,
                    requested_vertical_rate=requested_vertical_rate,
                    propulsion_bank_angle=propulsion_bank_angle,
                    load_factor=load_factor)
                return EnergyResult(**values).validate()
            raise EvaluationError('Installed pyBADA model needs a version-specific bluesky_energy adapter')
        except Exception as exc:
            raise EvaluationError(
                f'{bs.traf.id[idx]}/{bs.traf.type[idx]} BADA{self.family} h={h:.1f} '
                f'TAS={tas:.3f} mass={mass:.1f} phase={phase} schedule={self.schedule}: {exc}') from exc

    @staticmethod
    def _bound_text(value):
        try:
            return f'{float(value):.2f}' if np.isfinite(value) else 'unknown'
        except (TypeError, ValueError):
            return 'unknown'

    @staticmethod
    def _bound_text_abs(value):
        try:
            return f'{abs(float(value)):.2f}' if np.isfinite(value) else 'unknown'
        except (TypeError, ValueError):
            return 'unknown'

    def update_dynamics(self, traffic, dt):
        speed_handled = np.zeros(traffic.ntraf, dtype=bool)
        vertical_handled = np.zeros(traffic.ntraf, dtype=bool)
        if hasattr(self, 'evaluation_speed_evolution'):
            self.evaluation_speed_evolution[:] = ''
            self.evaluation_speed_target_cas[:] = np.nan
            self.evaluation_speed_target_mach[:] = np.nan
            self.evaluation_speed_target_tas[:] = np.nan
        request = getattr(traffic, 'speed_request', None)
        if request is None:
            current_tas = np.asarray(traffic.tas, dtype=float).copy()
            request = SpeedStepRequest(
                target_tas=current_tas.copy(),
                requested_acceleration=np.zeros(traffic.ntraf),
                capture=np.ones(traffic.ntraf, dtype=bool),
                next_tas=current_tas.copy()).validate(traffic.ntraf)
        applied_acceleration = request.requested_acceleration.copy()
        applied_next_tas = request.next_tas.copy()
        applied_capture = request.capture.copy()
        # Performance is evaluated for every aircraft, like BlueSky's original
        # BADA implementation.  dyn_mode only decides whether those results
        # drive motion; KINEMATIC runs still retain usable performance/fuel data.
        for idx in range(traffic.ntraf):
            must_hold = False
            try:
                intent = self._capture_speed_intent(idx)
                self._active_speed_intent = (idx, intent)
                result = self._evaluate(idx, speed_intent=intent)
                self._record_speed_intent(idx, intent)
                if hasattr(self, 'evaluation_tas'):
                    self.evaluation_tas[idx] = traffic.tas[idx]
                    self.evaluation_alt[idx] = traffic.alt[idx]
                    self.evaluation_mass[idx] = self.mass[idx]
                    self.evaluation_temperature[idx] = traffic.Temp[idx]
                    self.evaluation_pressure_alt[idx] = traffic.pressure_alt[idx]
                    self.evaluation_timestep[idx] = dt
                    self.model_rocd[idx] = result.rocd
                self.thrust[idx], self.rated_thrust[idx], self.drag[idx], self.fuelflow[idx] = \
                    result.thrust, result.rated_thrust, result.drag, result.fuel_flow
                if hasattr(self, 'requested_acceleration'):
                    self.required_thrust[idx] = result.required_thrust
                    self.idle_thrust[idx] = result.idle_thrust
                    self.maximum_thrust[idx] = result.maximum_thrust
                    self.requested_acceleration[idx] = result.requested_acceleration
                    self.applied_acceleration[idx] = result.applied_acceleration
                    self.thrust_limited[idx] = result.thrust_limited
                    self.thrust_limitation_reason[idx] = result.limitation_reason
                    self.requested_vertical_rate[idx] = result.requested_vertical_rate
                    self.applied_vertical_rate[idx] = result.applied_vertical_rate
                    self.energy_share_factor[idx] = result.esf
                    self.energy_allocation_policy[idx] = result.allocation_policy
                    self.propulsion_bank_angle[idx] = result.propulsion_bank_angle
                    self.propulsion_load_factor[idx] = result.load_factor
                    speed_request = getattr(traffic, 'speed_request', None)
                    self.target_tas[idx] = (traffic.aporasas.tas[idx] if speed_request is None
                                            else speed_request.target_tas[idx])
                    if hasattr(self, 'speed_capture'):
                        self.speed_capture[idx] = request.capture[idx]
                if result.thrust_limited and self.strict and self.dyn_mode[idx] == 0:
                    required_thrust = (result.required_thrust
                                       if np.isfinite(result.required_thrust)
                                       else result.thrust)
                    raise EvaluationError(
                        f'horizontal request infeasible: {result.limitation_reason}; '
                        f'required={required_thrust:.3f} N, bounds='
                        f'{result.idle_thrust:.3f}..{result.maximum_thrust:.3f} N')
                candidate_vs = None
                if self.dyn_mode[idx] == 1:
                    enforced_vertical = None
                    target_tas = request.target_tas[idx]
                    delta_alt = traffic.aporasas.alt[idx] - traffic.alt[idx]
                    candidate_vs = result.applied_vertical_rate
                    # Capture a boundary approached by the model's signed rate.
                    # Do not reverse an infeasible climb/descent merely to chase it.
                    if candidate_vs * delta_alt > 0:
                        candidate_vs = np.sign(candidate_vs) * min(
                            abs(candidate_vs), abs(delta_alt) / dt)
                    elif delta_alt == 0:
                        candidate_vs = 0.0
                    if (hasattr(self, 'envelope_policy') and
                            set(self.envelope_checks[idx]).intersection(
                                {EnvelopeCheck.ROC_MAX, EnvelopeCheck.ROD_MAX}) and
                            parse_policy(self.envelope_policy[idx]) != EnvelopePolicy.OFF):
                        policy = parse_policy(self.envelope_policy[idx])
                        selected_vertical = tuple(
                            check for check in self.envelope_checks[idx]
                            if check in {EnvelopeCheck.ROC_MAX, EnvelopeCheck.ROD_MAX})
                        vertical = self.vertical_bounds(
                            idx, mass=self.mass[idx], tas=traffic.tas[idx],
                            speed_intent=intent)
                        if policy == EnvelopePolicy.ENFORCE:
                            enforced_vertical = (vertical, selected_vertical)
                        vertical_result = evaluate_vertical(
                            candidate_vs, vertical, selected_vertical)
                        if vertical_result.status == EnvelopeStatus.UNKNOWN:
                            raise EvaluationError(vertical_result.reason)
                        if vertical_result.status == EnvelopeStatus.INFEASIBLE:
                            requested = {'vertical_rate_m_s': float(candidate_vs)}
                            if policy == EnvelopePolicy.ENFORCE:
                                if EnvelopeCheck.ROC_MAX in selected_vertical:
                                    candidate_vs = min(candidate_vs, vertical.maximum_rocd)
                                if EnvelopeCheck.ROD_MAX in selected_vertical:
                                    candidate_vs = max(candidate_vs, vertical.minimum_rocd)
                                self._set_result(
                                    idx, vertical_result, policy, EnvelopeAction.LIMITED,
                                    requested, {'vertical_rate_m_s': float(candidate_vs)},
                                    source='dynamics', contributes=False)
                            else:
                                action = (EnvelopeAction.ABORTED
                                          if policy == EnvelopePolicy.ABORT
                                          else EnvelopeAction.ACCEPTED)
                                self._set_result(idx, vertical_result, policy, action,
                                                 requested, requested, source='state')
                                if action == EnvelopeAction.ABORTED:
                                    bs.sim.hold()
                                    continue
                        else:
                            self._set_result(idx, vertical_result, policy,
                                             EnvelopeAction.NONE, source='dynamics',
                                             contributes=False)
                    tas = float(traffic.tas[idx])
                    mass = float(self.mass[idx])
                    if dt <= 0 or tas <= 0 or mass <= 0:
                        raise EvaluationError('positive timestep, TAS and mass required for TEM')
                    direction = np.sign(target_tas - tas)
                    desired_a = direction * min(abs(float(request.requested_acceleration[idx])),
                                                abs(target_tas - tas) / dt)
                    # Sacrifice vertical rate towards level flight before speed
                    # tracking. Do not invent a reversal or exceed the already
                    # captured/limited vertical request to achieve acceleration.
                    low_w, high_w = min(0.0, candidate_vs), max(0.0, candidate_vs)
                    if enforced_vertical is not None:
                        bounds, checks = enforced_vertical
                        if EnvelopeCheck.ROD_MAX in checks:
                            low_w = max(low_w, bounds.minimum_rocd)
                        if EnvelopeCheck.ROC_MAX in checks:
                            high_w = min(high_w, bounds.maximum_rocd)
                    thrust, acceleration, candidate_vs, required, limited, reason = allocate_speed_priority(
                        tas=tas, mass=mass, drag=result.drag,
                        idle_thrust=result.idle_thrust, maximum_thrust=result.maximum_thrust,
                        requested_acceleration=desired_a, preferred_vertical_rate=candidate_vs,
                        minimum_vertical_rate=low_w, maximum_vertical_rate=high_w)
                    proposed_tas = tas + acceleration * dt
                    reaches_target = abs(proposed_tas - target_tas) <= 1e-10
                    if reaches_target:
                        proposed_tas = target_tas
                    fuel = result.fuel_flow
                    if thrust != result.thrust:
                        model = self.models[idx]
                        if not hasattr(model, 'bluesky_fuel'):
                            raise EvaluationError('speed-adapted thrust requires a fuel adapter')
                        fuel = self._call_configuration_aware(model.bluesky_fuel,
                            self._configuration_mode(idx), h=float(traffic.pressure_alt[idx]),
                            tas=tas, mass=mass, temperature=float(traffic.Temp[idx]),
                            pressure=float(traffic.p[idx]), phase=self._phase(idx), thrust=thrust)
                    result = replace(result, thrust=thrust, required_thrust=required,
                        fuel_flow=float(fuel), applied_acceleration=acceleration,
                        applied_vertical_rate=candidate_vs, allocation_policy='SPEED_PRIORITY',
                        thrust_limited=limited, limitation_reason=reason).validate()
                    if not np.isfinite(proposed_tas) or proposed_tas <= 0:
                        raise EvaluationError('TEM response produces non-positive TAS')
                    applied_acceleration[idx] = acceleration
                    applied_next_tas[idx] = proposed_tas
                    applied_capture[idx] = reaches_target
                    speed_handled[idx] = True
                    self.thrust[idx], self.fuelflow[idx] = result.thrust, result.fuel_flow
                    if hasattr(self, 'applied_acceleration'):
                        self.applied_acceleration[idx] = acceleration
                        self.applied_vertical_rate[idx] = candidate_vs
                        self.energy_allocation_policy[idx] = result.allocation_policy
                        self.required_thrust[idx] = result.required_thrust
                        self.thrust_limited[idx] = result.thrust_limited
                        self.thrust_limitation_reason[idx] = result.limitation_reason
                    if hasattr(self, 'speed_capture'):
                        self.speed_capture[idx] = reaches_target
                candidate_mass = self.mass[idx] - result.fuel_flow * dt
                if hasattr(self, 'envelope_policy'):
                    override = bool(self.mass_override[idx]) if hasattr(self, 'mass_override') else False
                    ok, reason = self.assign_mass(idx, candidate_mass, override, runtime=True)
                    if not ok:
                        must_hold = True
                        raise EvaluationError('fuel/mass update rejected: ' + reason)
                else:  # Compatibility for minimal third-party/test implementations.
                    if not np.isfinite(candidate_mass) or candidate_mass <= 0:
                        must_hold = True
                        raise EvaluationError('fuel consumption exhausts positive aircraft mass')
                    self.mass[idx] = candidate_mass
                if self.dyn_mode[idx] == 1:
                    traffic.vs[idx] = candidate_vs
                    vertical_handled[idx] = True
                self.invalid[idx] = False
            except (ModelUnavailable, EvaluationError) as exc:
                self._clear_speed_intent(idx)
                self.invalid[idx] = True
                self.failure_count[idx] += 1
                self.thrust[idx] = self.rated_thrust[idx] = self.drag[idx] = self.fuelflow[idx] = np.nan
                if hasattr(self, 'requested_acceleration'):
                    self.required_thrust[idx] = np.nan
                    self.idle_thrust[idx] = self.maximum_thrust[idx] = np.nan
                    self.requested_acceleration[idx] = self.applied_acceleration[idx] = np.nan
                    self.thrust_limited[idx] = False
                    self.thrust_limitation_reason[idx] = ''
                    self.requested_vertical_rate[idx] = np.nan
                    self.applied_vertical_rate[idx] = np.nan
                    self.energy_share_factor[idx] = np.nan
                    self.energy_allocation_policy[idx] = ''
                    self.propulsion_bank_angle[idx] = np.nan
                    self.propulsion_load_factor[idx] = np.nan
                    if hasattr(self, 'speed_capture'):
                        self.speed_capture[idx] = False
                if self.strict or must_hold:
                    message = (f'PYBADATEM strict evaluation failure: {exc}; simulation held. '
                               'If recording, use RECORDRESEARCH STOP to finalize partial evidence')
                    print(message)
                    from bluesky import stack
                    stack.echo(message)
                    bs.sim.hold()
                    # A strict failure stops propagation without terminating the BlueSky process.
                    break
            finally:
                self._active_speed_intent = None
        traffic.speed_result = SpeedStepResult(
            request=request,
            applied_acceleration=applied_acceleration,
            capture=applied_capture,
            next_tas=applied_next_tas).validate(traffic.ntraf)
        return speed_handled, vertical_handled

    def limits(self, intent_v, intent_vs, intent_h, ax):
        """Apply selected speed/Mach/altitude policies to resolved guidance."""
        applied_v = np.asarray(intent_v, dtype=float).copy()
        applied_vs = np.asarray(intent_vs, dtype=float).copy()
        applied_h = np.asarray(intent_h, dtype=float).copy()
        for idx in range(len(applied_v)):
            policy = parse_policy(self.envelope_policy[idx])
            checks = set(self.envelope_checks[idx])
            if policy == EnvelopePolicy.OFF or not checks.intersection({
                    EnvelopeCheck.LOW_SPEED, EnvelopeCheck.HIGH_SPEED,
                    EnvelopeCheck.MACH_MIN, EnvelopeCheck.MACH_MAX,
                    EnvelopeCheck.ALTITUDE_MAX, EnvelopeCheck.ROC_MAX,
                    EnvelopeCheck.ROD_MAX, EnvelopeCheck.BANK_ANGLE,
                    EnvelopeCheck.LOAD_FACTOR}):
                continue
            model = self.models[idx]
            try:
                requested_cas, requested_mach = model.bluesky_airdata(
                    h=float(bs.traf.pressure_alt[idx]), tas=float(applied_v[idx]),
                    temperature=float(bs.traf.Temp[idx]))
            except Exception as exc:
                raise RuntimeError(f'{bs.traf.id[idx]} guidance airdata unknown: {exc}') from exc
            vertical_direction = np.sign(float(applied_h[idx]) - float(bs.traf.alt[idx]))
            requested_signed_vs = vertical_direction * abs(float(applied_vs[idx]))
            requested_bank = (self.effective_bank_angle(idx)
                              if checks.intersection({EnvelopeCheck.BANK_ANGLE,
                                                      EnvelopeCheck.LOAD_FACTOR}) else 0.0)
            requested_load = (1.0 / np.cos(np.radians(abs(requested_bank)))
                              if abs(requested_bank) < 90.0 else np.inf)
            result, bounds, vertical, lateral = self.evaluate_envelope(
                idx, cas=requested_cas, mach=requested_mach,
                altitude=float(applied_h[idx]), vertical_rate=requested_signed_vs)
            current_result, _, _, _ = self.evaluate_envelope(idx)
            requested = {'tas_m_s': float(intent_v[idx]), 'cas_m_s': requested_cas,
                         'mach': requested_mach, 'altitude_m': float(intent_h[idx]),
                         'vertical_rate_m_s': requested_signed_vs,
                         'bank_angle_deg': requested_bank,
                         'load_factor': requested_load}
            combined = combine_results(current_result, result)
            if combined.status == EnvelopeStatus.UNKNOWN:
                raise RuntimeError(f'{bs.traf.id[idx]} guidance envelope unknown: {combined.reason}')
            state_action = (EnvelopeAction.ABORTED if policy == EnvelopePolicy.ABORT and
                            current_result.status == EnvelopeStatus.INFEASIBLE
                            else EnvelopeAction.ACCEPTED)
            recorded_state = current_result
            # ENFORCE records the current finding, then publishes the applied
            # guidance limit with its requested and applied values below.
            self._set_result(idx, recorded_state, policy, state_action,
                             {'cas_m_s': float(bs.traf.cas[idx]),
                              'mach': float(bs.traf.M[idx]),
                              'altitude_m': float(bs.traf.alt[idx]),
                              'vertical_rate_m_s': float(bs.traf.vs[idx]),
                              'bank_angle_deg': requested_bank,
                              'load_factor': requested_load},
                             {'cas_m_s': float(bs.traf.cas[idx]),
                              'mach': float(bs.traf.M[idx]),
                              'altitude_m': float(bs.traf.alt[idx]),
                              'vertical_rate_m_s': float(bs.traf.vs[idx]),
                              'bank_angle_deg': requested_bank,
                              'load_factor': requested_load},
                             source='state', publish=policy != EnvelopePolicy.ENFORCE)
            if state_action == EnvelopeAction.ABORTED:
                bs.sim.hold()
                break
            if combined.status == EnvelopeStatus.VALID:
                self._set_result(idx, result, policy, EnvelopeAction.ACCEPTED,
                                 requested, requested, source='guidance')
                continue
            if policy == EnvelopePolicy.ENFORCE:
                failed = set(combined.failed_checks)
                if combined.status == EnvelopeStatus.INFEASIBLE:
                    if failed.intersection({EnvelopeCheck.LOW_SPEED, EnvelopeCheck.MACH_MIN}):
                        applied_v[idx] = max(applied_v[idx], bounds.minimum_tas)
                    if failed.intersection({EnvelopeCheck.HIGH_SPEED, EnvelopeCheck.MACH_MAX}):
                        applied_v[idx] = min(applied_v[idx], bounds.maximum_tas)
                    if EnvelopeCheck.ALTITUDE_MAX in result.failed_checks:
                        applied_h[idx] = min(
                            applied_h[idx], self._geometric_ceiling(
                                idx, bounds.maximum_altitude, applied_h[idx]))
                    if EnvelopeCheck.ROC_MAX in failed:
                        requested_signed_vs = min(requested_signed_vs,
                                                  vertical.maximum_rocd)
                    if EnvelopeCheck.ROD_MAX in failed:
                        requested_signed_vs = max(requested_signed_vs,
                                                  vertical.minimum_rocd)
                    applied_vs[idx] = abs(requested_signed_vs)
                    if failed.intersection({EnvelopeCheck.BANK_ANGLE,
                                            EnvelopeCheck.LOAD_FACTOR}):
                        maximum = np.radians(lateral.maximum_bank_angle_deg)
                        if float(bs.traf.ap.turnphi[idx]) > float(bs.traf.eps[idx]) ** 2:
                            selected = float(bs.traf.ap.turnphi[idx])
                            bs.traf.ap.turnphi[idx] = np.copysign(
                                min(abs(selected), maximum), selected)
                        else:
                            selected = float(bs.traf.ap.bankdef[idx])
                            bs.traf.ap.bankdef[idx] = np.copysign(
                                min(abs(selected), maximum), selected)
                applied_cas, applied_mach = model.bluesky_airdata(
                    h=float(bs.traf.pressure_alt[idx]), tas=float(applied_v[idx]),
                    temperature=float(bs.traf.Temp[idx]))
                applied_bank = (self.effective_bank_angle(idx)
                                if checks.intersection({EnvelopeCheck.BANK_ANGLE,
                                                        EnvelopeCheck.LOAD_FACTOR})
                                else requested_bank)
                applied = {'tas_m_s': float(applied_v[idx]), 'cas_m_s': applied_cas,
                           'mach': applied_mach, 'altitude_m': float(applied_h[idx]),
                           'vertical_rate_m_s': requested_signed_vs,
                           'bank_angle_deg': applied_bank,
                           'load_factor': (1.0 / np.cos(np.radians(abs(applied_bank))))}
                self._set_result(idx, combined, policy, EnvelopeAction.LIMITED,
                                 requested, applied, source='guidance', contributes=False)
            else:
                action = (EnvelopeAction.ABORTED if policy == EnvelopePolicy.ABORT
                          else EnvelopeAction.ACCEPTED)
                self._set_result(idx, result, policy, action, requested, requested,
                                 source='guidance', contributes=True)
                if action == EnvelopeAction.ABORTED:
                    bs.sim.hold()
        return applied_v, applied_vs, applied_h
