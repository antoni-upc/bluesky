"""BlueSky performance implementation for longitudinal/vertical TEM."""

import numpy as np

import bluesky as bs
from bluesky.traffic.performance.perfbase import PerfBase
from .model import EnergyResult, EvaluationError, ModelStore, ModelUnavailable
from .envelope import (EnvelopeAction, EnvelopeCheck, EnvelopePolicy, EnvelopeProfile,
                       EnvelopeResult, EnvelopeStatus, FlightBounds, QualityEvent, combine_results,
                       evaluate_flight, evaluate_mass, expand_checks, mass_bounds,
                       parse_policy, quality_events)


bs.settings.set_variable_defaults(
    pybada3_data_path='', pybada4_data_path='', pybada_family='4',
    pybada3_version='', pybada4_version='',
    pybada_strict=False, pybada_aircraft_aliases={}, pybada_speed_schedule='ICAO',
    pybada_envelope_policy='OFF', pybada_envelope_profile='LONGITUDINAL',
    pybada_envelope_checks=[])


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
        self.envelope_state_failed_checks = []
        self.envelope_guidance_failed_checks = []
        with self.settrafarrays():
            self.dyn_mode = np.array([], dtype=int)
            self.rated_thrust = np.array([])
            self.mass_override = np.array([], dtype=bool)
            self.invalid = np.array([], dtype=bool)
            self.failure_count = np.array([], dtype=int)
            self.envelope_policy = np.array([], dtype='U8')
            self.envelope_profile = np.array([], dtype='U12')
            self.envelope_status = np.array([], dtype='U10')
            self.envelope_last_action = np.array([], dtype='U8')
            self.envelope_last_reason = np.array([], dtype='U80')
            self.envelope_active_reason = np.array([], dtype='U80')
            self.envelope_state_reason = np.array([], dtype='U80')
            self.envelope_guidance_reason = np.array([], dtype='U80')
            self.envelope_attempt_reason = np.array([], dtype='U80')
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
                evaluation, _ = self.evaluate_envelope(idx, model=model)
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
            self.envelope_state_failed_checks.append(())
            self.envelope_guidance_failed_checks.append(())
        self.envelope_policy[-n:] = parse_policy(bs.settings.pybada_envelope_policy).value
        self.envelope_profile[-n:] = str(bs.settings.pybada_envelope_profile).upper()
        self.envelope_status[-n:] = EnvelopeStatus.VALID.value
        self.envelope_last_action[-n:] = EnvelopeAction.NONE.value
        self.envelope_active_reason[-n:] = ''
        self.envelope_state_reason[-n:] = ''
        self.envelope_guidance_reason[-n:] = ''
        self.envelope_attempt_reason[-n:] = ''
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
            del self.envelope_state_failed_checks[i]
            del self.envelope_guidance_failed_checks[i]
        super().delete(idx)

    def reset(self):
        self.models.clear()
        self.resolutions.clear()
        self.envelope_checks.clear()
        self.envelope_failed_checks.clear()
        self.envelope_state_failed_checks.clear()
        self.envelope_guidance_failed_checks.clear()
        super().reset()

    def bounds(self, idx):
        return mass_bounds(self.models[idx])

    def _phase(self, idx):
        target = bs.traf.aporasas.alt[idx]
        return ('Climb' if target > bs.traf.alt[idx] + 1.0 else
                ('Descent' if target < bs.traf.alt[idx] - 1.0 else 'Cruise'))

    def flight_bounds(self, idx, *, mass=None, cas=None, mach=None, model=None):
        model = model or self.models[idx]
        try:
            values = model.bluesky_envelope(
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

    def evaluate_envelope(self, idx, *, mass=None, cas=None, mach=None,
                          altitude=None, checks=None, model=None):
        checks = self.envelope_checks[idx] if checks is None else tuple(checks)
        candidate_mass = self.mass[idx] if mass is None else mass
        mbounds = mass_bounds(model or self.models[idx])
        flight_checks = {EnvelopeCheck.LOW_SPEED, EnvelopeCheck.HIGH_SPEED,
                         EnvelopeCheck.MACH_MIN, EnvelopeCheck.MACH_MAX,
                         EnvelopeCheck.ALTITUDE_MAX}
        fbounds = (self.flight_bounds(idx, mass=candidate_mass, cas=cas,
                                     mach=mach, model=model)
                   if set(checks).intersection(flight_checks)
                   else FlightBounds('', None, None, None, None, None))
        return combine_results(
            evaluate_mass(candidate_mass, mbounds, checks),
            evaluate_flight(
                bs.traf.cas[idx] if cas is None else cas,
                bs.traf.M[idx] if mach is None else mach,
                bs.traf.alt[idx] if altitude is None else altitude,
                fbounds, checks)), fbounds

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
        print(f'QUALITY aircraft={event.aircraft} component={event.component} '
              f'reason={reason} policy={event.policy} action={event.action} '
              f'continuation={event.continuation}')
        # ``print`` is retained for detached/headless evidence; stack.echo
        # publishes the same event to the interactive BlueSky console.
        from bluesky import stack
        stack.echo(f'QUALITY aircraft={event.aircraft} component={event.component} '
                   f'reason={reason} policy={event.policy} action={event.action} '
                   f'continuation={event.continuation}')
        quality_events.emit(event)
        return event

    def _refresh_envelope_status(self, idx):
        failed = list(self.envelope_state_failed_checks[idx])
        if self.envelope_guidance_infeasible[idx]:
            failed.extend(self.envelope_guidance_failed_checks[idx])
        self.envelope_failed_checks[idx] = tuple(dict.fromkeys(failed))
        self.envelope_status[idx] = (EnvelopeStatus.INFEASIBLE.value
                                     if failed else EnvelopeStatus.VALID.value)
        reasons = [self.envelope_state_reason[idx]]
        if self.envelope_guidance_infeasible[idx]:
            reasons.append(self.envelope_guidance_reason[idx])
        self.envelope_active_reason[idx] = ','.join(filter(None, reasons))

    def _set_result(self, idx, result, policy, action, requested=None, applied=None,
                    source='state', contributes=True):
        if source == 'state':
            reason_array = self.envelope_state_reason
            failed_list = self.envelope_state_failed_checks
        elif source == 'guidance':
            reason_array = self.envelope_guidance_reason
            failed_list = self.envelope_guidance_failed_checks
        else:
            reason_array = self.envelope_attempt_reason
            failed_list = None
        previous_reason = reason_array[idx]
        other_active_reason = ''
        if source == 'state' and self.envelope_guidance_infeasible[idx]:
            other_active_reason = self.envelope_guidance_reason[idx]
        elif source == 'guidance':
            other_active_reason = self.envelope_state_reason[idx]
        reason_array[idx] = '' if result.status == EnvelopeStatus.VALID else result.reason
        if failed_list is not None:
            failed_list[idx] = (() if result.status == EnvelopeStatus.VALID
                                else result.failed_checks)
        if source == 'guidance':
            self.envelope_guidance_infeasible[idx] = (
                contributes and result.status == EnvelopeStatus.INFEASIBLE)
        self._refresh_envelope_status(idx)
        if (result.status != EnvelopeStatus.VALID and previous_reason != result.reason
                and (source == 'attempt' or other_active_reason != result.reason)):
            self._emit_event(idx, result.reason, action, requested, applied)

    def _clear_envelope_sources(self, idx):
        self.envelope_state_reason[idx] = ''
        self.envelope_guidance_reason[idx] = ''
        self.envelope_attempt_reason[idx] = ''
        self.envelope_state_failed_checks[idx] = ()
        self.envelope_guidance_failed_checks[idx] = ()
        self.envelope_guidance_infeasible[idx] = False
        self._refresh_envelope_status(idx)

    def configure_envelope(self, idx, *, policy=None, profile=None, checks=None):
        old = (self.envelope_policy[idx], self.envelope_profile[idx], self.envelope_checks[idx])
        new_policy = parse_policy(policy or old[0])
        from .envelope import parse_profile
        new_profile = parse_profile(profile or old[1])
        new_checks = tuple(checks) if checks is not None else (
            self.envelope_checks[idx] if profile is None else expand_checks(new_profile))
        result = EnvelopeStatus.VALID
        evaluation, _ = self.evaluate_envelope(idx, checks=new_checks)
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
            self._set_result(idx, evaluation, new_policy, action, self.mass[idx], self.mass[idx])
            if evaluation.status == EnvelopeStatus.INFEASIBLE and new_policy == EnvelopePolicy.ABORT:
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
        result, _ = self.evaluate_envelope(idx, mass=value)
        if result.status == EnvelopeStatus.UNKNOWN:
            return False, result.reason
        if result.status == EnvelopeStatus.INFEASIBLE and policy == EnvelopePolicy.ENFORCE:
            self._set_result(idx, result, policy, EnvelopeAction.REJECTED,
                             value, self.mass[idx], source='attempt', contributes=False)
            return False, result.reason
        self.mass[idx], self.mass_override[idx] = value, override
        self._set_result(idx, EnvelopeResult(EnvelopeStatus.VALID), policy,
                         EnvelopeAction.NONE, source='attempt', contributes=False)
        action = EnvelopeAction.ABORTED if policy == EnvelopePolicy.ABORT and result.status != EnvelopeStatus.VALID else EnvelopeAction.ACCEPTED
        self._set_result(idx, result, policy, action, value, value)
        if action == EnvelopeAction.ABORTED:
            bs.sim.hold()
        return True, ''

    def assess_direct_state(self, idx, previous):
        """Assess a synchronized provisional MOVE state transactionally."""
        policy = parse_policy(self.envelope_policy[idx])
        result, bounds = self.evaluate_envelope(
            idx, checks=() if policy == EnvelopePolicy.OFF else None)
        if result.status == EnvelopeStatus.UNKNOWN:
            return False, f'reason={result.reason}; prior state preserved'
        if policy == EnvelopePolicy.OFF:
            self.envelope_status[idx] = EnvelopeStatus.VALID.value
            return True, ''
        requested = {'cas_m_s': float(bs.traf.cas[idx]), 'mach': float(bs.traf.M[idx]),
                     'altitude_m': float(bs.traf.alt[idx])}
        applied = (None if previous is None else
                   {'cas_m_s': float(previous['cas']), 'mach': float(previous['M']),
                    'altitude_m': float(previous['alt'])})
        if result.status == EnvelopeStatus.INFEASIBLE and policy == EnvelopePolicy.ENFORCE:
            self._set_result(idx, result, policy, EnvelopeAction.REJECTED,
                             requested, applied, source='attempt', contributes=False)
            return False, (f'policy=ENFORCE, reason={result.reason}, '
                           f'CAS bounds={bounds.minimum_cas}..{bounds.maximum_cas} m/s, '
                           f'Mach bounds={bounds.minimum_mach}..{bounds.maximum_mach}, '
                           f'altitude max={bounds.maximum_altitude} m; prior state preserved')
        action = (EnvelopeAction.ABORTED if policy == EnvelopePolicy.ABORT and
                  result.status == EnvelopeStatus.INFEASIBLE else EnvelopeAction.ACCEPTED)
        self._set_result(idx, EnvelopeResult(EnvelopeStatus.VALID), policy,
                         EnvelopeAction.NONE, source='attempt', contributes=False)
        self._set_result(idx, result, policy, action, requested, requested)
        if action == EnvelopeAction.ABORTED:
            bs.sim.hold()
        return True, ''

    def _evaluate(self, idx):
        """Evaluate the pyBADA API through one observable failure boundary."""
        ac = self.models[idx]
        h, tas, mass = bs.traf.pressure_alt[idx], bs.traf.tas[idx], self.mass[idx]
        phase = self._phase(idx)
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
                candidate_mass = self.mass[idx] - result.fuel_flow * dt
                if hasattr(self, 'envelope_policy'):
                    override = bool(self.mass_override[idx]) if hasattr(self, 'mass_override') else False
                    ok, reason = self.assign_mass(idx, candidate_mass, override, runtime=True)
                    if not ok and parse_policy(self.envelope_policy[idx]) != EnvelopePolicy.ENFORCE:
                        raise EvaluationError(reason)
                else:  # Compatibility for minimal third-party/test implementations.
                    self.mass[idx] = max(1.0, candidate_mass)
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

    def limits(self, intent_v, intent_vs, intent_h, ax):
        """Apply selected speed/Mach/altitude policies to resolved guidance."""
        applied_v = np.asarray(intent_v, dtype=float).copy()
        applied_h = np.asarray(intent_h, dtype=float).copy()
        for idx in range(len(applied_v)):
            policy = parse_policy(self.envelope_policy[idx])
            checks = set(self.envelope_checks[idx])
            if policy == EnvelopePolicy.OFF or not checks.intersection({
                    EnvelopeCheck.LOW_SPEED, EnvelopeCheck.HIGH_SPEED,
                    EnvelopeCheck.MACH_MIN, EnvelopeCheck.MACH_MAX,
                    EnvelopeCheck.ALTITUDE_MAX}):
                continue
            model = self.models[idx]
            try:
                requested_cas, requested_mach = model.bluesky_airdata(
                    h=float(bs.traf.pressure_alt[idx]), tas=float(applied_v[idx]),
                    temperature=float(bs.traf.Temp[idx]))
            except Exception as exc:
                raise RuntimeError(f'{bs.traf.id[idx]} guidance airdata unknown: {exc}') from exc
            result, bounds = self.evaluate_envelope(
                idx, cas=requested_cas, mach=requested_mach,
                altitude=float(applied_h[idx]))
            current_result, _ = self.evaluate_envelope(idx)
            requested = {'tas_m_s': float(intent_v[idx]), 'cas_m_s': requested_cas,
                         'mach': requested_mach, 'altitude_m': float(intent_h[idx])}
            combined = combine_results(current_result, result)
            if combined.status == EnvelopeStatus.UNKNOWN:
                raise RuntimeError(f'{bs.traf.id[idx]} guidance envelope unknown: {combined.reason}')
            state_action = (EnvelopeAction.ABORTED if policy == EnvelopePolicy.ABORT and
                            current_result.status == EnvelopeStatus.INFEASIBLE
                            else EnvelopeAction.ACCEPTED)
            self._set_result(idx, current_result, policy, state_action,
                             {'cas_m_s': float(bs.traf.cas[idx]),
                              'mach': float(bs.traf.M[idx]),
                              'altitude_m': float(bs.traf.alt[idx])},
                             {'cas_m_s': float(bs.traf.cas[idx]),
                              'mach': float(bs.traf.M[idx]),
                              'altitude_m': float(bs.traf.alt[idx])},
                             source='state')
            if state_action == EnvelopeAction.ABORTED:
                bs.sim.hold()
                break
            if combined.status == EnvelopeStatus.VALID:
                self._set_result(idx, result, policy, EnvelopeAction.ACCEPTED,
                                 requested, requested, source='guidance')
                continue
            if policy == EnvelopePolicy.ENFORCE:
                if result.status == EnvelopeStatus.INFEASIBLE:
                    if checks.intersection({EnvelopeCheck.LOW_SPEED, EnvelopeCheck.MACH_MIN}):
                        applied_v[idx] = max(applied_v[idx], bounds.minimum_tas)
                    if checks.intersection({EnvelopeCheck.HIGH_SPEED, EnvelopeCheck.MACH_MAX}):
                        applied_v[idx] = min(applied_v[idx], bounds.maximum_tas)
                    if EnvelopeCheck.ALTITUDE_MAX in checks:
                        applied_h[idx] = min(applied_h[idx], bounds.maximum_altitude)
                applied_cas, applied_mach = model.bluesky_airdata(
                    h=float(bs.traf.pressure_alt[idx]), tas=float(applied_v[idx]),
                    temperature=float(bs.traf.Temp[idx]))
                applied = {'tas_m_s': float(applied_v[idx]), 'cas_m_s': applied_cas,
                           'mach': applied_mach, 'altitude_m': float(applied_h[idx])}
                self._set_result(idx, result, policy, EnvelopeAction.LIMITED,
                                 requested, applied, source='guidance', contributes=False)
            else:
                action = (EnvelopeAction.ABORTED if policy == EnvelopePolicy.ABORT
                          else EnvelopeAction.ACCEPTED)
                self._set_result(idx, result, policy, action, requested, requested,
                                 source='guidance', contributes=True)
                if action == EnvelopeAction.ABORTED:
                    bs.sim.hold()
        return applied_v, np.asarray(intent_vs, dtype=float), applied_h
