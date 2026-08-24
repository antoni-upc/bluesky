from types import SimpleNamespace

import numpy as np
import pytest

import bluesky as bs
from bluesky.plugins.pybada.envelope import (
    EnvelopeAction, EnvelopeCheck, EnvelopePolicy, EnvelopeProfile, EnvelopeResult,
    EnvelopeStatus, FlightBounds, LateralBounds, VerticalBounds,
    evaluate_flight, evaluate_lateral, evaluate_mass, evaluate_vertical, expand_checks, mass_bounds,
    parse_checks)
from bluesky.plugins.pybada.performance import PyBadaTEM
from bluesky.plugins.pybada.model import EvaluationError


def make_perf(policies=('OFF', 'OFF')):
    perf = object.__new__(PyBadaTEM)
    perf.schedule = 'ICAO'
    perf.models = [SimpleNamespace(OEW=40_000.0, MTOW=80_000.0) for _ in policies]
    perf.mass = np.full(len(policies), 60_000.0)
    perf.mass_override = np.zeros(len(policies), dtype=bool)
    perf.envelope_policy = np.asarray(policies, dtype='U8')
    perf.envelope_profile = np.asarray(['CUSTOM'] * len(policies), dtype='U12')
    perf.envelope_checks = [(EnvelopeCheck.MASS_MIN, EnvelopeCheck.MASS_MAX)
                            for _ in policies]
    perf.envelope_failed_checks = [() for _ in policies]
    perf.envelope_mass_failed_checks = [() for _ in policies]
    perf.envelope_state_failed_checks = [() for _ in policies]
    perf.envelope_guidance_failed_checks = [() for _ in policies]
    perf.envelope_status = np.asarray(['VALID'] * len(policies), dtype='U10')
    perf.envelope_last_action = np.asarray(['NONE'] * len(policies), dtype='U8')
    perf.envelope_last_reason = np.asarray([''] * len(policies), dtype='U80')
    perf.envelope_active_reason = np.asarray([''] * len(policies), dtype='U80')
    perf.envelope_mass_reason = np.asarray([''] * len(policies), dtype='U80')
    perf.envelope_state_reason = np.asarray([''] * len(policies), dtype='U80')
    perf.envelope_guidance_reason = np.asarray([''] * len(policies), dtype='U80')
    perf.envelope_attempt_reason = np.asarray([''] * len(policies), dtype='U80')
    perf.envelope_dynamics_reason = np.asarray([''] * len(policies), dtype='U80')
    perf.envelope_guidance_infeasible = np.zeros(len(policies), dtype=bool)
    perf.envelope_event_count = np.zeros(len(policies), dtype=int)
    perf.envelope_violation_count = np.zeros(len(policies), dtype=int)
    return perf


def traffic(monkeypatch):
    autopilot = SimpleNamespace(turnphi=np.zeros(2),
                                bankdef=np.radians(np.array([25.0, 25.0])))
    monkeypatch.setattr(bs, 'traf', SimpleNamespace(
        id=['A1', 'A2'], cas=np.array([120.0, 120.0]), M=np.array([0.4, 0.4]),
        alt=np.array([3000.0, 3000.0]), pressure_alt=np.array([3000.0, 3000.0]),
        vs=np.array([0.0, 0.0]), tas=np.array([120.0, 120.0]),
        hdg=np.array([90.0, 90.0]),
        Temp=np.array([268.0, 268.0]), p=np.array([70000.0, 70000.0]),
        swhdgsel=np.array([False, False]), eps=np.full(2, 1e-6), ap=autopilot,
        aporasas=SimpleNamespace(alt=np.array([3000.0, 3000.0]),
                                hdg=np.array([90.0, 90.0]))))
    monkeypatch.setattr(bs, 'sim', SimpleNamespace(simt=4.0, hold=lambda: None))


def test_profiles_custom_validation_and_bounds():
    assert expand_checks('CORE_ONLY') == ()
    assert EnvelopeCheck.MASS_MIN in expand_checks('LONGITUDINAL')
    assert EnvelopeCheck.BANK_ANGLE not in expand_checks('LONGITUDINAL')
    assert EnvelopeCheck.BANK_ANGLE in expand_checks('FULL')
    assert parse_checks(('MASS_MIN,MASS_MAX',)) == (
        EnvelopeCheck.MASS_MIN, EnvelopeCheck.MASS_MAX)
    with pytest.raises(ValueError, match='duplicate'):
        parse_checks(('MASS_MIN', 'MASS_MIN'))
    with pytest.raises(ValueError, match='unknown'):
        parse_checks(('NOT_A_CHECK',))
    assert mass_bounds(SimpleNamespace(OEW=4.0, MTOW=8.0)).known
    assert not mass_bounds(SimpleNamespace(OEW=9.0, MTOW=8.0)).known


def test_mass_policy_matrix_is_transactional_and_isolated(monkeypatch):
    traffic(monkeypatch)
    perf = make_perf(('OFF', 'ENFORCE'))
    assert perf.assign_mass(0, 90_000.0)[0]
    ok, reason = perf.assign_mass(1, 90_000.0)
    assert not ok and reason == 'MASS_MAX'
    assert perf.mass.tolist() == [90_000.0, 60_000.0]
    assert perf.envelope_status.tolist() == ['VALID', 'VALID']
    assert perf.envelope_event_count.tolist() == [0, 1]
    # Persistent rejected state emits once; recovery permits recurrence.
    assert not perf.assign_mass(1, 90_000.0)[0]
    assert perf.envelope_event_count[1] == 1
    assert perf.assign_mass(1, 70_000.0)[0]
    assert not perf.assign_mass(1, 90_000.0)[0]
    assert perf.envelope_event_count[1] == 2


def test_report_accepts_and_recovers_without_cross_aircraft_mutation(monkeypatch):
    traffic(monkeypatch)
    perf = make_perf(('REPORT', 'OFF'))
    assert perf.assign_mass(0, 90_000.0)[0]
    assert perf.mass.tolist() == [90_000.0, 60_000.0]
    assert perf.envelope_status.tolist() == ['INFEASIBLE', 'VALID']
    assert perf.envelope_failed_checks[0] == (EnvelopeCheck.MASS_MAX,)
    assert perf.assign_mass(0, 70_000.0)[0]
    assert perf.envelope_status[0] == EnvelopeStatus.VALID.value
    assert perf.envelope_last_reason[0] == 'MASS_MAX'


def test_runtime_mass_update_does_not_duplicate_active_state_transition(monkeypatch):
    traffic(monkeypatch)
    perf = make_perf(('OFF',))
    assert perf.assign_mass(0, 90_000.0)[0]
    assert perf.configure_envelope(0, policy=EnvelopePolicy.REPORT)[0]
    assert perf.envelope_event_count[0] == 1
    assert perf.assign_mass(0, 89_999.0, runtime=True)[0]
    assert perf.envelope_event_count[0] == 1


def test_quality_event_is_published_to_interactive_console(monkeypatch):
    traffic(monkeypatch)
    messages = []
    monkeypatch.setattr('bluesky.stack.echo', messages.append)
    perf = make_perf(('REPORT', 'OFF'))
    assert perf.assign_mass(0, 90_000.0)[0]
    assert len(messages) == 1
    assert 'aircraft=A1' in messages[0]
    assert 'reason=MASS_MAX' in messages[0]
    assert 'policy=REPORT' in messages[0]
    assert 'requested={mass_kg=90000.00}' in messages[0]
    assert 'applied={mass_kg=90000.00}' in messages[0]


def test_unknown_bounds_reject_enabled_configuration_without_mutation(monkeypatch):
    traffic(monkeypatch)
    perf = make_perf(('OFF',))
    perf.models[0] = SimpleNamespace(OEW=40_000.0)
    old = (perf.envelope_policy[0], perf.envelope_profile[0], perf.envelope_checks[0])
    ok, reason = perf.configure_envelope(0, policy=EnvelopePolicy.REPORT)
    assert not ok and 'OEW/MTOW' in reason
    assert (perf.envelope_policy[0], perf.envelope_profile[0], perf.envelope_checks[0]) == old


def test_envelope_configuration_event_reports_current_failed_state(monkeypatch):
    traffic(monkeypatch)
    messages = []
    monkeypatch.setattr('bluesky.stack.echo', messages.append)
    perf = make_perf(('OFF',))
    failed = EnvelopeResult(EnvelopeStatus.INFEASIBLE,
                            (EnvelopeCheck.LOW_SPEED,), 'LOW_SPEED')
    monkeypatch.setattr(perf, 'evaluate_envelope',
                        lambda *args, **kwargs: (failed, None, None, None))
    success, _ = perf.configure_envelope(
        0, policy=EnvelopePolicy.REPORT,
        profile=EnvelopeProfile.CUSTOM, checks=(EnvelopeCheck.LOW_SPEED,))
    assert success
    assert 'requested={tas_m_s=120.00,cas_m_s=120.00}' in messages[0]
    assert 'value=60000' not in messages[0]


def test_badaconfig_failure_preserves_prior_mode(monkeypatch):
    perf = make_perf(('OFF',))
    perf.bada_configuration_mode = np.array(['PYBADA'], dtype='U8')

    def reject(*args, **kwargs):
        raise EvaluationError('CR evaluation unavailable')

    monkeypatch.setattr(perf, '_evaluate', reject)
    success, reason = perf.configure_bada_configuration(0, 'CRUISE')
    assert not success
    assert 'CR evaluation unavailable' in reason
    assert perf.bada_configuration_mode[0] == 'PYBADA'


def test_fundamental_mass_validation_is_never_disabled(monkeypatch):
    traffic(monkeypatch)
    perf = make_perf()
    for value in (0.0, -1.0, np.nan, np.inf):
        assert not perf.assign_mass(0, value)[0]
    assert perf.mass[0] == 60_000.0


def test_selected_minimum_can_be_disabled():
    result = evaluate_mass(30_000.0, mass_bounds(SimpleNamespace(OEW=40_000.0, MTOW=80_000.0)),
                           (EnvelopeCheck.MASS_MAX,))
    assert result.status == EnvelopeStatus.VALID


class FakeFlightModel:
    OEW = 40_000.0
    MTOW = 80_000.0

    def bluesky_airdata(self, *, h, tas, temperature):
        return float(tas), float(tas / 300.0)

    def bluesky_envelope(self, **state):
        return dict(configuration='CR', minimum_cas=100.0, maximum_cas=200.0,
                    minimum_mach=1.0 / 3.0, maximum_mach=2.0 / 3.0,
                    maximum_altitude=10_000.0, minimum_tas=100.0,
                    maximum_tas=200.0)

    def bluesky_vertical_envelope(self, **state):
        return dict(minimum_rocd=-8.0, maximum_rocd=6.0)

    def bluesky_lateral_envelope(self, **state):
        return dict(configuration=state['configuration'], minimum_load_factor=-1.0,
                    maximum_load_factor=2.0, maximum_bank_angle_deg=60.0)


def test_flight_evaluation_reports_selected_longitudinal_reasons():
    bounds = FlightBounds('CR', 100.0, 200.0, 1 / 3, 2 / 3, 10_000.0)
    result = evaluate_flight(220.0, 0.75, 11_000.0, bounds, (
        EnvelopeCheck.HIGH_SPEED, EnvelopeCheck.MACH_MAX,
        EnvelopeCheck.ALTITUDE_MAX))
    assert result.status == EnvelopeStatus.INFEASIBLE
    assert result.failed_checks == (EnvelopeCheck.HIGH_SPEED,
                                    EnvelopeCheck.MACH_MAX,
                                    EnvelopeCheck.ALTITUDE_MAX)


def test_vertical_evaluation_uses_signed_rocd_and_rejects_unknown_bounds():
    bounds = VerticalBounds(-8.0, 6.0)
    assert evaluate_vertical(7.0, bounds, (EnvelopeCheck.ROC_MAX,)).reason == 'ROC_MAX'
    assert evaluate_vertical(-9.0, bounds, (EnvelopeCheck.ROD_MAX,)).reason == 'ROD_MAX'
    assert evaluate_vertical(0.0, bounds, tuple()).status == EnvelopeStatus.VALID
    unknown = evaluate_vertical(1.0, VerticalBounds(None, 6.0),
                                (EnvelopeCheck.ROD_MAX,))
    assert unknown.status == EnvelopeStatus.UNKNOWN
    assert evaluate_vertical(6.005, bounds, (EnvelopeCheck.ROC_MAX,)).status == \
        EnvelopeStatus.VALID
    assert evaluate_vertical(6.02, bounds, (EnvelopeCheck.ROC_MAX,)).reason == 'ROC_MAX'


def test_lateral_evaluation_uses_selected_bada_bounds():
    bounds = LateralBounds('CR', -1.0, 2.5, 66.4218215)
    result = evaluate_lateral(75.0, 3.8637, bounds, (
        EnvelopeCheck.BANK_ANGLE, EnvelopeCheck.LOAD_FACTOR))
    assert result.status == EnvelopeStatus.INFEASIBLE
    assert result.failed_checks == (EnvelopeCheck.BANK_ANGLE,
                                    EnvelopeCheck.LOAD_FACTOR)
    assert evaluate_lateral(60.0, 2.0, bounds, (
        EnvelopeCheck.BANK_ANGLE, EnvelopeCheck.LOAD_FACTOR)).status == \
        EnvelopeStatus.VALID
    unknown = evaluate_lateral(10.0, 1.02,
                               LateralBounds('CR', None, None, None),
                               (EnvelopeCheck.LOAD_FACTOR,))
    assert unknown.status == EnvelopeStatus.UNKNOWN


def test_lateral_guidance_report_and_enforce_are_isolated(monkeypatch):
    traffic(monkeypatch)
    monkeypatch.setattr('bluesky.stack.echo', lambda message: None)
    bs.traf.swhdgsel[:] = True
    bs.traf.ap.bankdef[:] = np.radians(75.0)
    perf = make_perf(('REPORT', 'ENFORCE'))
    perf.models = [FakeFlightModel(), FakeFlightModel()]
    selected = (EnvelopeCheck.BANK_ANGLE, EnvelopeCheck.LOAD_FACTOR)
    perf.envelope_checks = [selected, selected]
    perf.limits(np.array([120.0, 120.0]), np.zeros(2),
                np.array([3000.0, 3000.0]), np.zeros(2))
    assert np.degrees(bs.traf.ap.bankdef).tolist() == pytest.approx([75.0, 60.0])
    assert perf.envelope_status.tolist() == ['INFEASIBLE', 'VALID']
    assert perf.envelope_last_action.tolist() == ['ACCEPTED', 'LIMITED']
    assert perf.envelope_event_count.tolist() == [1, 1]
    perf.limits(np.array([120.0, 120.0]), np.zeros(2),
                np.array([3000.0, 3000.0]), np.zeros(2))
    assert perf.envelope_event_count.tolist() == [1, 1]


def test_vertical_guidance_report_and_enforce_are_isolated(monkeypatch):
    traffic(monkeypatch)
    monkeypatch.setattr('bluesky.stack.echo', lambda message: None)
    perf = make_perf(('REPORT', 'ENFORCE'))
    perf.models = [FakeFlightModel(), FakeFlightModel()]
    selected = (EnvelopeCheck.ROC_MAX, EnvelopeCheck.ROD_MAX)
    perf.envelope_checks = [selected, selected]
    requested_vs = np.array([20.0, 20.0])
    _, applied_vs, _ = perf.limits(
        np.array([120.0, 120.0]), requested_vs,
        np.array([6000.0, 0.0]), np.zeros(2))
    np.testing.assert_allclose(applied_vs, [20.0, 8.0])
    np.testing.assert_allclose(requested_vs, [20.0, 20.0])
    assert perf.envelope_status.tolist() == ['INFEASIBLE', 'VALID']
    assert perf.envelope_last_reason.tolist() == ['ROC_MAX', 'ROD_MAX']
    assert perf.envelope_event_count.tolist() == [1, 1]
    perf.limits(np.array([120.0, 120.0]), requested_vs,
                np.array([6000.0, 0.0]), np.zeros(2))
    assert perf.envelope_event_count.tolist() == [1, 1]


def test_enforce_recovers_current_vertical_overshoot_as_a_limit(monkeypatch):
    traffic(monkeypatch)
    bs.traf.vs[0] = -20.0
    messages = []
    monkeypatch.setattr('bluesky.stack.echo', messages.append)
    perf = make_perf(('ENFORCE', 'OFF'))
    perf.models = [FakeFlightModel(), FakeFlightModel()]
    perf.envelope_checks[0] = (EnvelopeCheck.ROD_MAX,)
    _, applied_vs, _ = perf.limits(
        np.array([120.0, 120.0]), np.array([20.0, 0.0]),
        np.array([0.0, 3000.0]), np.zeros(2))
    assert applied_vs[0] == 8.0
    assert perf.envelope_status[0] == 'VALID'
    assert perf.envelope_last_action[0] == 'LIMITED'
    assert perf.envelope_last_reason[0] == 'ROD_MAX'
    assert perf.envelope_event_count[0] == 1
    assert ('requested={direction=DESCENT,vertical_rate_magnitude_m_s=20.00}'
            in messages[0])
    assert ('applied={direction=DESCENT,vertical_rate_magnitude_m_s=8.00}'
            in messages[0])


class FakeVerticalEnergyModel(FakeFlightModel):
    def bluesky_energy(self, **state):
        return dict(thrust=12_000.0, rated_thrust=14_000.0, drag=10_000.0,
                    fuel_flow=0.0, esf=0.5, rocd=20.0, acceleration=0.0,
                    applied_vertical_rate=20.0, allocation_policy='BADA_ESF')


def test_tem_output_is_checked_before_state_mutation(monkeypatch):
    traffic(monkeypatch)
    bs.traf.ntraf = 2
    bs.traf.type = ['A320', 'A320']
    bs.traf.ax = np.zeros(2)
    bs.traf.aporasas.alt[:] = 6000.0
    monkeypatch.setattr('bluesky.stack.echo', lambda message: None)
    perf = make_perf(('REPORT', 'ENFORCE'))
    perf.models = [FakeVerticalEnergyModel(), FakeVerticalEnergyModel()]
    perf.envelope_checks = [(EnvelopeCheck.ROC_MAX,), (EnvelopeCheck.ROC_MAX,)]
    perf.family = '4'
    perf.dyn_mode = np.ones(2, dtype=int)
    perf.thrust = np.zeros(2)
    perf.rated_thrust = np.zeros(2)
    perf.drag = np.zeros(2)
    perf.fuelflow = np.zeros(2)
    perf.invalid = np.zeros(2, dtype=bool)
    perf.failure_count = np.zeros(2, dtype=int)
    speed_handled, vertical_handled = perf.update_dynamics(bs.traf, 1.0)
    assert speed_handled.tolist() == [True, True]
    assert vertical_handled.tolist() == [True, True]
    np.testing.assert_allclose(bs.traf.vs, [20.0, 6.0])
    assert perf.envelope_status.tolist() == ['INFEASIBLE', 'VALID']
    assert perf.envelope_last_action.tolist() == ['ACCEPTED', 'LIMITED']
    assert perf.envelope_event_count.tolist() == [1, 1]
    speed_handled, vertical_handled = perf.update_dynamics(bs.traf, 1.0)
    assert speed_handled.tolist() == [True, True]
    assert vertical_handled.tolist() == [True, True]
    assert perf.envelope_event_count.tolist() == [1, 1]


def test_guidance_report_and_enforce_are_per_aircraft_and_atomic(monkeypatch):
    traffic(monkeypatch)
    monkeypatch.setattr('bluesky.stack.echo', lambda message: None)
    perf = make_perf(('REPORT', 'ENFORCE'))
    perf.models = [FakeFlightModel(), FakeFlightModel()]
    selected = (EnvelopeCheck.HIGH_SPEED, EnvelopeCheck.MACH_MAX,
                EnvelopeCheck.ALTITUDE_MAX)
    perf.envelope_checks = [selected, selected]
    requested_v = np.array([250.0, 250.0])
    requested_h = np.array([12_000.0, 12_000.0])
    applied_v, _, applied_h = perf.limits(
        requested_v, np.zeros(2), requested_h, np.zeros(2))
    np.testing.assert_allclose(applied_v, [250.0, 200.0])
    np.testing.assert_allclose(applied_h, [12_000.0, 10_000.0])
    assert perf.envelope_status.tolist() == ['INFEASIBLE', 'VALID']
    assert perf.envelope_event_count.tolist() == [1, 1]
    assert perf.envelope_last_action.tolist() == ['ACCEPTED', 'LIMITED']
    # Runtime fuel validation is a separate state source and must not recover
    # or retrigger a persistent guidance violation.
    assert perf.assign_mass(0, 59_999.0, runtime=True)[0]
    assert perf.assign_mass(1, 59_999.0, runtime=True)[0]
    assert perf.envelope_status.tolist() == ['INFEASIBLE', 'VALID']
    perf.limits(requested_v, np.zeros(2), requested_h, np.zeros(2))
    assert perf.envelope_event_count.tolist() == [1, 1]
    # Input guidance arrays are never partially mutated.
    np.testing.assert_allclose(requested_v, [250.0, 250.0])
    np.testing.assert_allclose(requested_h, [12_000.0, 12_000.0])


def test_unknown_selected_flight_bound_rejects_without_partial_limiting(monkeypatch):
    traffic(monkeypatch)
    perf = make_perf(('ENFORCE', 'OFF'))
    perf.models[0] = SimpleNamespace(OEW=40_000.0, MTOW=80_000.0)
    perf.envelope_checks[0] = (EnvelopeCheck.HIGH_SPEED,)
    requested_v = np.array([250.0, 150.0])
    requested_h = np.array([3000.0, 3000.0])
    with pytest.raises(RuntimeError, match='A1 guidance airdata unknown'):
        perf.limits(requested_v, np.zeros(2), requested_h, np.zeros(2))
    np.testing.assert_allclose(requested_v, [250.0, 150.0])
    np.testing.assert_allclose(requested_h, [3000.0, 3000.0])


def test_same_persistent_reason_across_state_and_guidance_emits_once(monkeypatch):
    traffic(monkeypatch)
    monkeypatch.setattr('bluesky.stack.echo', lambda message: None)
    perf = make_perf(('REPORT', 'OFF'))
    violation = EnvelopeResult(EnvelopeStatus.INFEASIBLE,
                               (EnvelopeCheck.ALTITUDE_MAX,), 'ALTITUDE_MAX')
    perf._set_result(0, violation, EnvelopePolicy.REPORT, EnvelopeAction.ACCEPTED,
                     source='state')
    perf._set_result(0, violation, EnvelopePolicy.REPORT, EnvelopeAction.ACCEPTED,
                     source='guidance')
    assert perf.envelope_event_count[0] == 1
    assert perf.envelope_status[0] == 'INFEASIBLE'
