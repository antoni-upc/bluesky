"""BlueSky performance implementation for longitudinal/vertical TEM."""

import numpy as np

import bluesky as bs
from bluesky.traffic.performance.perfbase import PerfBase
from .model import (EnergyResult, EvaluationError, ModelStore, ModelUnavailable,
                    parse_configuration_mode)
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


class PyBadaTEM(PerfBase):
    """One authoritative BADA 3/4 integration with native lateral guidance."""

    requires_synced_direct_state = True

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
            self.idle_thrust = np.array([])
            self.maximum_thrust = np.array([])
            self.target_tas = np.array([])
            self.requested_acceleration = np.array([])
            self.applied_acceleration = np.array([])
            self.thrust_limited = np.array([], dtype=bool)
            self.thrust_limitation_reason = np.array([], dtype='U32')
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
        return ('Climb' if target > bs.traf.alt[idx] + 1.0 else
                ('Descent' if target < bs.traf.alt[idx] - 1.0 else 'Cruise'))

    def _configuration_mode(self, idx, override=None):
        if override is not None:
            return parse_configuration_mode(override).value
        values = getattr(self, 'bada_configuration_mode', None)
        return ('PYBADA' if values is None or idx >= len(values)
                else parse_configuration_mode(values[idx]).value)

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

    def vertical_bounds(self, idx, *, mass=None, tas=None, model=None,
                        configuration_mode=None):
        model = model or self.models[idx]
        try:
            values = self._call_configuration_aware(model.bluesky_vertical_envelope,
                self._configuration_mode(idx, configuration_mode),
                h=float(bs.traf.pressure_alt[idx]),
                tas=float(bs.traf.tas[idx] if tas is None else tas),
                mass=float(self.mass[idx] if mass is None else mass),
                temperature=float(bs.traf.Temp[idx]), pressure=float(bs.traf.p[idx]),
                schedule=self.schedule)
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
        return combine_results(
            evaluate_mass(candidate_mass, mbounds, checks),
            evaluate_flight(
                bs.traf.cas[idx] if cas is None else cas,
                bs.traf.M[idx] if mach is None else mach,
                bs.traf.alt[idx] if altitude is None else altitude,
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
                    source='state', contributes=True):
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
        if (result.status != EnvelopeStatus.VALID and previous_reason != result.reason
                and (source == 'attempt' or result.reason not in other_active_reasons)):
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
                           f'altitude max={self._bound_text(bounds.maximum_altitude)} m, '
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

    def _evaluate(self, idx, configuration_mode=None):
        """Evaluate the pyBADA API through one observable failure boundary."""
        ac = self.models[idx]
        h, tas, mass = bs.traf.pressure_alt[idx], bs.traf.tas[idx], self.mass[idx]
        phase = self._phase(idx)
        speed_request = getattr(bs.traf, 'speed_request', None)
        requested_acceleration = (0.0 if speed_request is None else
                                  float(speed_request.requested_acceleration[idx]))
        try:
            # Adapter-friendly hook used by dependency-free fakes and future
            # pyBADA-version-specific adapters.
            if hasattr(ac, 'bluesky_energy'):
                values = self._call_configuration_aware(ac.bluesky_energy,
                    self._configuration_mode(idx, configuration_mode),
                    h=h, tas=tas, mass=mass,
                    temperature=bs.traf.Temp[idx], pressure=bs.traf.p[idx], phase=phase,
                    schedule=self.schedule,
                    requested_acceleration=requested_acceleration)
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
        # Performance is evaluated for every aircraft, like BlueSky's original
        # BADA implementation.  dyn_mode only decides whether those results
        # drive motion; KINEMATIC runs still retain usable performance/fuel data.
        for idx in range(traffic.ntraf):
            try:
                result = self._evaluate(idx)
                self.thrust[idx], self.rated_thrust[idx], self.drag[idx], self.fuelflow[idx] = \
                    result.thrust, result.rated_thrust, result.drag, result.fuel_flow
                if hasattr(self, 'requested_acceleration'):
                    self.idle_thrust[idx] = result.idle_thrust
                    self.maximum_thrust[idx] = result.maximum_thrust
                    self.requested_acceleration[idx] = result.requested_acceleration
                    self.applied_acceleration[idx] = result.applied_acceleration
                    self.thrust_limited[idx] = result.thrust_limited
                    self.thrust_limitation_reason[idx] = result.limitation_reason
                    speed_request = getattr(traffic, 'speed_request', None)
                    self.target_tas[idx] = (traffic.aporasas.tas[idx] if speed_request is None
                                            else speed_request.target_tas[idx])
                if result.thrust_limited and self.strict:
                    raise EvaluationError(
                        f'horizontal request infeasible: {result.limitation_reason}; '
                        f'required={result.thrust:.3f} N, bounds='
                        f'{result.idle_thrust:.3f}..{result.maximum_thrust:.3f} N')
                candidate_vs = None
                if self.dyn_mode[idx] == 1:
                    delta_alt = traffic.aporasas.alt[idx] - traffic.alt[idx]
                    candidate_vs = np.sign(delta_alt) * min(
                        abs(result.rocd), abs(delta_alt) / dt)
                    if (hasattr(self, 'envelope_policy') and
                            set(self.envelope_checks[idx]).intersection(
                                {EnvelopeCheck.ROC_MAX, EnvelopeCheck.ROD_MAX}) and
                            parse_policy(self.envelope_policy[idx]) != EnvelopePolicy.OFF):
                        policy = parse_policy(self.envelope_policy[idx])
                        selected_vertical = tuple(
                            check for check in self.envelope_checks[idx]
                            if check in {EnvelopeCheck.ROC_MAX, EnvelopeCheck.ROD_MAX})
                        vertical = self.vertical_bounds(
                            idx, mass=self.mass[idx], tas=traffic.tas[idx])
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
                candidate_mass = self.mass[idx] - result.fuel_flow * dt
                if hasattr(self, 'envelope_policy'):
                    override = bool(self.mass_override[idx]) if hasattr(self, 'mass_override') else False
                    ok, reason = self.assign_mass(idx, candidate_mass, override, runtime=True)
                    if not ok and parse_policy(self.envelope_policy[idx]) != EnvelopePolicy.ENFORCE:
                        raise EvaluationError(reason)
                else:  # Compatibility for minimal third-party/test implementations.
                    self.mass[idx] = max(1.0, candidate_mass)
                if self.dyn_mode[idx] == 1:
                    traffic.vs[idx] = candidate_vs
                    vertical_handled[idx] = True
                self.invalid[idx] = False
            except (ModelUnavailable, EvaluationError) as exc:
                self.invalid[idx] = True
                self.failure_count[idx] += 1
                self.thrust[idx] = self.rated_thrust[idx] = self.drag[idx] = self.fuelflow[idx] = np.nan
                if hasattr(self, 'requested_acceleration'):
                    self.idle_thrust[idx] = self.maximum_thrust[idx] = np.nan
                    self.requested_acceleration[idx] = self.applied_acceleration[idx] = np.nan
                    self.thrust_limited[idx] = False
                    self.thrust_limitation_reason[idx] = ''
                if self.strict:
                    raise RuntimeError(f'PYBADATEM strict evaluation failure: {exc}') from exc
        # BlueSky retains horizontal speed ownership so SPD commands and
        # waypoint constraints use its native selected-speed capture logic.
        # TEM owns only vertical speed; pyBADA performance and fuel evaluation
        # still run for every aircraft.
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
            recorded_state = (EnvelopeResult(EnvelopeStatus.VALID)
                              if policy == EnvelopePolicy.ENFORCE else current_result)
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
                             source='state')
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
                    if EnvelopeCheck.ALTITUDE_MAX in failed:
                        applied_h[idx] = min(applied_h[idx], bounds.maximum_altitude)
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
