from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from bluesky.plugins.pybada.model import (BadaModelAdapter, EnergyResult,
                                          EvaluationError, ModelStore, ModelUnavailable,
                                          _clamp_thrust)
from bluesky.plugins.pybada.performance import PyBadaTEM


def test_energy_result_rejects_missing_or_negative_physics():
    EnergyResult(10, 12, 8, 0.2, 1, 2, 0.1).validate()
    with pytest.raises(EvaluationError):
        EnergyResult(10, 12, 8, -0.2, 1, 2, 0.1).validate()
    with pytest.raises(EvaluationError):
        EnergyResult(float('nan'), 12, 8, 0.2, 1, 2, 0.1).validate()


def test_unavailable_idle_does_not_poison_finite_climb_thrust():
    thrust, limited, reason, idle, maximum = _clamp_thrust(
        140_000.0, float('nan'), 140_000.0)
    assert thrust == 140_000.0
    assert not limited and reason == ''
    assert np.isnan(idle)
    assert maximum == 140_000.0


def test_strict_resolution_only_exact_or_alias(tmp_path):
    (tmp_path / 'A320.OPF').touch()
    store = ModelStore('3', str(tmp_path), version='3.15', strict=True)
    with pytest.raises(ModelUnavailable):
        store.resolve('A32X')


def test_interactive_resolution_is_not_prefix_matching(tmp_path):
    (tmp_path / 'A320.OPF').touch()
    store = ModelStore('3', str(tmp_path), version='3.15', strict=False)
    with pytest.raises(ModelUnavailable):
        store.resolve('A32X')


def test_bada3_fixed_width_code_normalization(tmp_path):
    store = ModelStore('3', str(tmp_path), version='3.15', strict=True)
    candidate, method = store._candidate('A320', {'A320__': 'A320__'})
    assert candidate == 'A320__'
    assert method == 'bada3-code'
    candidate, _ = store._candidate('A32', {'A320__': 'A320__'})
    assert candidate is None


def test_dataset_version_is_required(tmp_path):
    with pytest.raises(ModelUnavailable, match='version is required'):
        ModelStore('3', str(tmp_path))


def test_activation_requires_path_and_version(monkeypatch):
    perf = object.__new__(PyBadaTEM)
    perf.family = '4'
    perf.strict = True
    monkeypatch.setattr('bluesky.plugins.pybada.performance.bs.settings.pybada4_data_path', '')
    monkeypatch.setattr('bluesky.plugins.pybada.performance.bs.settings.pybada4_version', '')
    with pytest.raises(ModelUnavailable, match='Configure both pybada4_data_path'):
        perf.activate('4')


def test_failed_family_switch_is_transactional(monkeypatch):
    old_store = object()
    old_model = object()
    old_resolution = object()
    perf = object.__new__(PyBadaTEM)
    perf.family = '4'
    perf.version = '4.2'
    perf.strict = True
    perf.store = old_store
    perf.models = [old_model]
    perf.resolutions = [old_resolution]

    class RejectingStore:
        version = '3.15'

        def __init__(self, *args, **kwargs):
            pass

        def resolve(self, actype):
            raise ModelUnavailable(f'No BADA 3 model for {actype}')

    monkeypatch.setattr('bluesky.plugins.pybada.performance.ModelStore', RejectingStore)
    monkeypatch.setattr('bluesky.plugins.pybada.performance.bs.traf',
                        SimpleNamespace(type=['A320-232']))
    monkeypatch.setattr('bluesky.plugins.pybada.performance.bs.settings.pybada3_data_path',
                        '/configured/bada3')
    monkeypatch.setattr('bluesky.plugins.pybada.performance.bs.settings.pybada3_version', '3.15')

    with pytest.raises(ModelUnavailable):
        perf.activate('3')
    assert perf.family == '4'
    assert perf.version == '4.2'
    assert perf.store is old_store
    assert perf.models == [old_model]
    assert perf.resolutions == [old_resolution]


def test_create_validation_rejects_before_mutating_performance_state(tmp_path):
    (tmp_path / 'A320.OPF').touch()
    perf = object.__new__(PyBadaTEM)
    perf.store = ModelStore('3', str(tmp_path), version='3.15', strict=True)
    perf.models = []
    perf.resolutions = []
    valid, message = perf.validate_create(['A32X'])
    assert not valid
    assert 'A32X' in message
    assert perf.models == []
    assert perf.resolutions == []


class FakeAtmosphere:
    @staticmethod
    def cas2Mach(*, cas, **kwargs):
        return cas / 300.0

    @staticmethod
    def cas2Tas(*, cas, **kwargs):
        return cas


class FakeBada3Envelope:
    def getConfig(self, **kwargs):
        return 'CR'

    def VMin(self, *, h, mass, config, deltaTemp):
        return 100.0

    def VMax(self, *, h, deltaTemp):
        return 200.0

    def maxAltitude(self, *, mass, deltaTemp):
        return 11_000.0

    def getBankAngle(self, *, phase, flightUnit, value):
        assert (phase, flightUnit, value) == ('cl', 'civ', 'max')
        return 30.0


class FakeBada4Envelope:
    def getConfig(self, **kwargs):
        return 'CR'

    def getAeroConfig(self, *, config):
        return 0, 'LGUP'

    def VMin(self, *, config, theta, delta, mass):
        return 105.0

    def VMax(self, *, h, HLid, LG, delta, theta, mass, nz):
        return 205.0

    def maxAltitude(self, *, HLid, LG, M, deltaTemp, mass, nz):
        return 12_000.0

    def maxM(self, *, LG):
        return 0.78


class FakePartialBada4Envelope(FakeBada4Envelope):
    def VMax(self, **kwargs):
        return None


@pytest.mark.parametrize(('family', 'aircraft', 'expected'), [
    ('3', SimpleNamespace(flightEnvelope=FakeBada3Envelope(), MMO=0.77),
     (100.0, 200.0, 11_000.0, 0.77)),
    ('4', SimpleNamespace(flightEnvelope=FakeBada4Envelope()),
     (105.0, 205.0, 12_000.0, 0.78)),
])
def test_family_specific_longitudinal_envelope_adapter(monkeypatch, family, aircraft, expected):
    monkeypatch.setattr(BadaModelAdapter, '_atmosphere', staticmethod(
        lambda h, tas, temperature: (FakeAtmosphere(), 2.0, 0.9, 0.7, 0.8, 0.5)))
    result = BadaModelAdapter(aircraft, family).bluesky_envelope(
        h=3000.0, cas=150.0, mach=0.5, mass=60_000.0,
        temperature=270.0, pressure=70_000.0, phase='Cruise')
    assert (result['minimum_cas'], result['maximum_cas'],
            result['maximum_altitude'], result['maximum_mach']) == expected
    assert result['minimum_mach'] == pytest.approx(expected[0] / 300.0)


def test_unavailable_speed_bound_does_not_hide_available_altitude_bound(monkeypatch):
    monkeypatch.setattr(BadaModelAdapter, '_atmosphere', staticmethod(
        lambda h, tas, temperature: (FakeAtmosphere(), 2.0, 0.9, 0.7, 0.8, 0.5)))
    aircraft = SimpleNamespace(flightEnvelope=FakePartialBada4Envelope())
    result = BadaModelAdapter(aircraft, '4').bluesky_envelope(
        h=14_000.0, cas=130.0, mach=0.5, mass=60_000.0,
        temperature=220.0, pressure=14_000.0, phase='Cruise')
    assert result['maximum_cas'] is None
    assert result['maximum_tas'] is None
    assert result['maximum_altitude'] == 12_000.0


def test_configuration_mode_selects_pybada_or_forced_cruise(monkeypatch):
    class AdaptiveEnvelope(FakeBada4Envelope):
        def getConfig(self, **kwargs):
            return 'AP'

        def getAeroConfig(self, *, config):
            return (1, 'LGUP') if config == 'AP' else (0, 'LGUP')

    monkeypatch.setattr(BadaModelAdapter, '_atmosphere', staticmethod(
        lambda h, tas, temperature: (FakeAtmosphere(), 2.0, 0.9, 0.7, 0.8, 0.5)))
    adapter = BadaModelAdapter(SimpleNamespace(flightEnvelope=AdaptiveEnvelope()), '4')
    state = dict(h=1000.0, cas=75.0, mach=0.25, mass=60_000.0,
                 temperature=280.0, pressure=90_000.0, phase='Descent')
    assert adapter.bluesky_envelope(**state, configuration_mode='PYBADA')[
        'configuration'] == 'AP'
    assert adapter.bluesky_envelope(**state, configuration_mode='CRUISE')[
        'configuration'] == 'CR'


def test_bada3_lateral_adapter_uses_phase_bank_limit():
    aircraft = SimpleNamespace(flightEnvelope=FakeBada3Envelope())
    result = BadaModelAdapter(aircraft, '3').bluesky_lateral_envelope(
        configuration='CR', phase='Climb')
    assert result['maximum_bank_angle_deg'] == 30.0
    assert result['maximum_load_factor'] == pytest.approx(
        1.0 / np.cos(np.radians(30.0)))
    assert result['minimum_load_factor'] is None


def test_bada3_lateral_adapter_handles_pybada_0112_gpf_keyword_bug():
    class BrokenEnvelope:
        AC = SimpleNamespace(GPFdata=[
            {'name': 'ang_bank_max', 'value': 43.0,
             'engine': ['jet'], 'phase': ['cl', 'cr', 'des'], 'flight': 'civ'},
            {'name': 'ang_bank_max', 'value': 71.0,
             'engine': ['jet'], 'phase': ['cl', 'cr', 'des'], 'flight': 'mil'},
        ])

        def getBankAngle(self, **kwargs):
            raise TypeError("getGPFValue() got an unexpected keyword argument 'flightUnit'")

    result = BadaModelAdapter(
        SimpleNamespace(flightEnvelope=BrokenEnvelope()), '3').bluesky_lateral_envelope(
            configuration='CR', phase='Descent')
    assert result['maximum_bank_angle_deg'] == 43.0
    assert result['maximum_load_factor'] == pytest.approx(
        1.0 / np.cos(np.radians(43.0)))
    assert result['minimum_load_factor'] is None


def test_bada3_lateral_adapter_uses_observed_terminal_configuration_phase():
    class TerminalEnvelope(FakeBada3Envelope):
        def getBankAngle(self, *, phase, flightUnit, value):
            assert (phase, flightUnit, value) == ('lnd', 'civ', 'max')
            return 27.0

    result = BadaModelAdapter(
        SimpleNamespace(flightEnvelope=TerminalEnvelope()), '3').bluesky_lateral_envelope(
            configuration='LD', phase='Descent')
    assert result['maximum_bank_angle_deg'] == 27.0
    assert result['maximum_load_factor'] == pytest.approx(
        1.0 / np.cos(np.radians(27.0)))


def test_bada4_lateral_adapter_selects_clean_and_high_lift_dlm(tmp_path):
    aircraft_dir = tmp_path / 'A320-232'
    aircraft_dir.mkdir()
    source = aircraft_dir / 'A320-232.xml'
    source.write_text('<Aircraft><DLM><n1>2.5</n1><n3>-1.0</n3>'
                      '<nf1>2.0</nf1><nf3>0.0</nf3></DLM></Aircraft>')

    class ConfigEnvelope:
        def getAeroConfig(self, *, config):
            return (0, 'LGUP') if config == 'CR' else (1, 'LGUP')

    aircraft = SimpleNamespace(filePath=str(tmp_path), acName='A320-232',
                               flightEnvelope=ConfigEnvelope())
    adapter = BadaModelAdapter(aircraft, '4')
    clean = adapter.bluesky_lateral_envelope(configuration='CR', phase='Cruise')
    high_lift = adapter.bluesky_lateral_envelope(configuration='AP', phase='Descent')
    assert (clean['minimum_load_factor'], clean['maximum_load_factor']) == (-1.0, 2.5)
    assert (clean['high_lift_id'], clean['landing_gear']) == (0.0, 'LGUP')
    assert (clean['minimum_limit_name'], clean['maximum_limit_name']) == ('n3', 'n1')
    assert clean['maximum_bank_angle_deg'] == pytest.approx(
        np.degrees(np.arccos(1.0 / 2.5)))
    assert (high_lift['minimum_load_factor'], high_lift['maximum_load_factor']) == (0.0, 2.0)
    assert (high_lift['high_lift_id'], high_lift['landing_gear']) == (1.0, 'LGUP')
    assert (high_lift['minimum_limit_name'], high_lift['maximum_limit_name']) == ('nf3', 'nf1')
    assert high_lift['maximum_bank_angle_deg'] == pytest.approx(60.0)
    source.unlink()
    assert adapter.bluesky_lateral_envelope(
        configuration='CR', phase='Cruise')['maximum_load_factor'] == 2.5


def test_vertical_envelope_uses_lidl_and_mcmb_at_same_operating_point(monkeypatch):
    adapter = BadaModelAdapter(SimpleNamespace(), '4')
    calls = []

    def energy(**state):
        calls.append(state)
        return {'rocd': -7.5 if state['phase'] == 'Descent' else 5.25}

    monkeypatch.setattr(adapter, 'bluesky_energy', energy)
    result = adapter.bluesky_vertical_envelope(
        h=3000.0, tas=150.0, mass=60_000.0, temperature=270.0,
        pressure=70_000.0, schedule='ICAO')
    assert result == {'minimum_rocd': -7.5, 'maximum_rocd': 5.25}
    assert [call['phase'] for call in calls] == ['Descent', 'Climb']
    assert calls[0]['h'] == calls[1]['h'] == 3000.0
    assert calls[0]['mass'] == calls[1]['mass'] == 60_000.0


@pytest.mark.smoke
@pytest.mark.parametrize(('family', 'dummy_name'), [('3', 'J2M___'), ('4', 'Dummy-TWIN')])
def test_packaged_dummy_uses_equilibrium_cruise_and_rated_phase_thrust(
        family, dummy_name):
    pybada = pytest.importorskip('pyBADA')
    data_path = Path(pybada.__file__).parent / 'aircraft' / f'BADA{family}' / 'DUMMY'
    version = '3.15' if family == '3' else '4.2'
    model, resolution = ModelStore(family, str(data_path), version=version).resolve('A320')
    assert resolution.resolved == dummy_name
    state = dict(h=3048.0, tas=148.526, mass=64791.0,
                 temperature=268.338, pressure=69676.8, schedule='ICAO')

    cruise = EnergyResult(**model.bluesky_energy(phase='Cruise', **state)).validate()
    assert cruise.thrust == pytest.approx(cruise.drag)
    assert cruise.rated_thrust > cruise.thrust
    assert cruise.rocd == 0.0

    climb = EnergyResult(**model.bluesky_energy(phase='Climb', **state)).validate()
    assert climb.thrust == pytest.approx(climb.rated_thrust)
    assert climb.rocd > 0.0

    descent = EnergyResult(**model.bluesky_energy(phase='Descent', **state)).validate()
    assert descent.thrust == pytest.approx(descent.rated_thrust)
    assert descent.rocd < 0.0


@pytest.mark.smoke
@pytest.mark.parametrize(('family', 'dummy_name'), [('3', 'J2M___'), ('4', 'Dummy-TWIN')])
def test_packaged_dummy_cruise_adapted_thrust_matches_requested_acceleration(
        family, dummy_name):
    pybada = pytest.importorskip('pyBADA')
    data_path = Path(pybada.__file__).parent / 'aircraft' / f'BADA{family}' / 'DUMMY'
    version = '3.15' if family == '3' else '4.2'
    model, resolution = ModelStore(family, str(data_path), version=version).resolve('A320')
    assert resolution.resolved == dummy_name
    mass = 64791.0
    requested = 0.1
    result = EnergyResult(**model.bluesky_energy(
        h=3048.0, tas=148.526, mass=mass, temperature=268.338,
        pressure=69676.8, phase='Cruise', schedule='ICAO',
        requested_acceleration=requested)).validate()
    assert (result.thrust - result.drag) / mass == pytest.approx(requested)
    assert result.requested_acceleration == pytest.approx(requested)
    assert result.applied_acceleration == pytest.approx(requested)
    assert result.fuel_flow >= 0.0
    assert np.isfinite(result.idle_thrust)
    assert np.isfinite(result.maximum_thrust)


@pytest.mark.smoke
@pytest.mark.parametrize(('family', 'dummy_name'), [('3', 'J2M___'), ('4', 'Dummy-TWIN')])
def test_packaged_dummy_turn_load_increases_drag_and_closes_force_balance(
        family, dummy_name):
    pybada = pytest.importorskip('pyBADA')
    data_path = Path(pybada.__file__).parent / 'aircraft' / f'BADA{family}' / 'DUMMY'
    version = '3.15' if family == '3' else '4.2'
    model, resolution = ModelStore(family, str(data_path), version=version).resolve('A320')
    assert resolution.resolved == dummy_name
    state = dict(h=3048.0, tas=148.526, mass=64791.0,
                 temperature=268.338, pressure=69676.8, phase='Cruise',
                 schedule='ICAO', requested_acceleration=0.1)
    straight = EnergyResult(**model.bluesky_energy(**state)).validate()
    bank = 35.0
    load = 1.0 / np.cos(np.radians(bank))
    turning = EnergyResult(**model.bluesky_energy(
        **state, propulsion_bank_angle=bank, load_factor=load)).validate()
    assert straight.load_factor == 1.0
    assert turning.load_factor == pytest.approx(load)
    assert turning.propulsion_bank_angle == pytest.approx(bank)
    assert turning.drag > straight.drag
    assert turning.thrust > straight.thrust
    assert (turning.thrust - turning.drag) / state['mass'] == pytest.approx(0.1)


def test_energy_adapter_rejects_inconsistent_or_invalid_turn_load():
    adapter = BadaModelAdapter(SimpleNamespace(), '4')
    state = dict(h=3048.0, tas=148.526, mass=64791.0,
                 temperature=268.338, pressure=69676.8, phase='Cruise',
                 schedule='ICAO')
    with pytest.raises(EvaluationError, match='inconsistent'):
        adapter.bluesky_energy(**state, propulsion_bank_angle=30.0, load_factor=1.0)
    with pytest.raises(EvaluationError, match='finite and physical'):
        adapter.bluesky_energy(**state, propulsion_bank_angle=90.0,
                               load_factor=float('inf'))


@pytest.mark.smoke
@pytest.mark.parametrize(('family', 'dummy_name'), [('3', 'J2M___'), ('4', 'Dummy-TWIN')])
@pytest.mark.parametrize(('requested', 'bound_name', 'reason'), [
    (10.0, 'maximum_thrust', 'ABOVE_MAXIMUM_THRUST'),
    (-10.0, 'idle_thrust', 'BELOW_IDLE_THRUST'),
])
def test_packaged_dummy_cruise_clamps_thrust_and_reports_feasible_acceleration(
        family, dummy_name, requested, bound_name, reason):
    pybada = pytest.importorskip('pyBADA')
    data_path = Path(pybada.__file__).parent / 'aircraft' / f'BADA{family}' / 'DUMMY'
    version = '3.15' if family == '3' else '4.2'
    model, resolution = ModelStore(family, str(data_path), version=version).resolve('A320')
    assert resolution.resolved == dummy_name
    mass = 64791.0
    result = EnergyResult(**model.bluesky_energy(
        h=3048.0, tas=148.526, mass=mass, temperature=268.338,
        pressure=69676.8, phase='Cruise', schedule='ICAO',
        requested_acceleration=requested)).validate()
    assert result.thrust_limited
    assert result.limitation_reason == reason
    assert result.thrust == pytest.approx(getattr(result, bound_name))
    assert result.applied_acceleration == pytest.approx(
        (result.thrust - result.drag) / mass)
    assert abs(result.applied_acceleration) < abs(requested)
    assert result.required_thrust != pytest.approx(result.thrust)


@pytest.mark.smoke
@pytest.mark.parametrize(('family', 'dummy_name'), [('3', 'J2M___'), ('4', 'Dummy-TWIN')])
@pytest.mark.parametrize(('phase', 'requested_vs'), [('Climb', 8.0), ('Descent', -6.0)])
def test_packaged_dummy_bada_esf_joint_energy_balance(
        family, dummy_name, phase, requested_vs):
    pybada = pytest.importorskip('pyBADA')
    from pyBADA import atmosphere as atm
    from pyBADA import constants

    data_path = Path(pybada.__file__).parent / 'aircraft' / f'BADA{family}' / 'DUMMY'
    version = '3.15' if family == '3' else '4.2'
    model, resolution = ModelStore(family, str(data_path), version=version).resolve('A320')
    assert resolution.resolved == dummy_name
    state = dict(h=3048.0, tas=148.526, mass=64791.0,
                 temperature=268.338, pressure=69676.8, schedule='ICAO')
    result = EnergyResult(**model.bluesky_energy(
        phase=phase, requested_acceleration=2.0,
        requested_vertical_rate=requested_vs, **state)).validate()
    delta_temp = atm.ISATemperatureDeviation(
        temperature=state['temperature'], pressureAltitude=state['h'])
    temperature_factor = (state['temperature'] - delta_temp) / state['temperature']
    specific_power = (result.thrust - result.drag) * state['tas'] / state['mass']
    allocated_power = (state['tas'] * result.applied_acceleration +
                       constants.g * result.applied_vertical_rate / temperature_factor)
    assert allocated_power == pytest.approx(specific_power, rel=1e-10, abs=1e-10)
    assert result.requested_vertical_rate == pytest.approx(requested_vs)
    assert result.applied_vertical_rate == pytest.approx(result.rocd)
    assert result.allocation_policy == 'BADA_ESF'


@pytest.mark.smoke
@pytest.mark.parametrize(('family', 'dummy_name'), [('3', 'J2M___'), ('4', 'Dummy-TWIN')])
def test_packaged_dummy_bada_esf_deceleration_descent_closes_signed_energy_balance(
        family, dummy_name):
    pybada = pytest.importorskip('pyBADA')
    from pyBADA import atmosphere as atm
    from pyBADA import constants

    data_path = Path(pybada.__file__).parent / 'aircraft' / f'BADA{family}' / 'DUMMY'
    version = '3.15' if family == '3' else '4.2'
    model, resolution = ModelStore(family, str(data_path), version=version).resolve('A320')
    assert resolution.resolved == dummy_name
    state = dict(h=3657.6, tas=148.526, mass=64791.0,
                 temperature=264.374, pressure=64440.0, schedule='ICAO')
    requested_acceleration = -2.0
    requested_vertical_rate = -8.0
    result = EnergyResult(**model.bluesky_energy(
        phase='Descent', requested_acceleration=requested_acceleration,
        requested_vertical_rate=requested_vertical_rate, **state)).validate()
    delta_temp = atm.ISATemperatureDeviation(
        temperature=state['temperature'], pressureAltitude=state['h'])
    temperature_factor = (state['temperature'] - delta_temp) / state['temperature']
    specific_power = (result.thrust - result.drag) * state['tas'] / state['mass']
    allocated_power = (state['tas'] * result.applied_acceleration +
                       constants.g * result.applied_vertical_rate / temperature_factor)
    assert result.requested_acceleration == pytest.approx(requested_acceleration)
    assert result.requested_vertical_rate == pytest.approx(requested_vertical_rate)
    assert result.applied_acceleration < 0.0
    assert result.applied_vertical_rate < 0.0
    assert result.applied_vertical_rate == pytest.approx(result.rocd)
    assert result.thrust == pytest.approx(result.idle_thrust)
    assert result.allocation_policy == 'BADA_ESF'
    assert allocated_power < 0.0
    assert allocated_power == pytest.approx(specific_power, rel=1e-10, abs=1e-10)


@pytest.mark.smoke
@pytest.mark.parametrize(('family', 'dummy_name'), [('3', 'J2M___'), ('4', 'Dummy-TWIN')])
def test_packaged_dummy_bada_esf_climb_overrides_conflicting_deceleration_request(
        family, dummy_name):
    pybada = pytest.importorskip('pyBADA')
    from pyBADA import atmosphere as atm
    from pyBADA import constants

    data_path = Path(pybada.__file__).parent / 'aircraft' / f'BADA{family}' / 'DUMMY'
    version = '3.15' if family == '3' else '4.2'
    model, resolution = ModelStore(family, str(data_path), version=version).resolve('A320')
    assert resolution.resolved == dummy_name
    state = dict(h=3048.0, tas=148.526, mass=64791.0,
                 temperature=268.338, pressure=69676.8, schedule='ICAO')
    requested_acceleration = -2.0
    requested_vertical_rate = 8.0
    result = EnergyResult(**model.bluesky_energy(
        phase='Climb', requested_acceleration=requested_acceleration,
        requested_vertical_rate=requested_vertical_rate, **state)).validate()
    delta_temp = atm.ISATemperatureDeviation(
        temperature=state['temperature'], pressureAltitude=state['h'])
    temperature_factor = (state['temperature'] - delta_temp) / state['temperature']
    specific_power = (result.thrust - result.drag) * state['tas'] / state['mass']
    requested_power = (state['tas'] * requested_acceleration +
                       constants.g * requested_vertical_rate / temperature_factor)
    allocated_power = (state['tas'] * result.applied_acceleration +
                       constants.g * result.applied_vertical_rate / temperature_factor)
    assert result.requested_acceleration == pytest.approx(requested_acceleration)
    assert result.requested_vertical_rate == pytest.approx(requested_vertical_rate)
    assert result.applied_acceleration > 0.0
    assert result.applied_vertical_rate > 0.0
    assert result.thrust == pytest.approx(result.maximum_thrust)
    assert result.allocation_policy == 'BADA_ESF'
    assert requested_power < 0.0 < allocated_power
    assert allocated_power == pytest.approx(specific_power, rel=1e-10, abs=1e-10)


@pytest.mark.smoke
@pytest.mark.parametrize(('family', 'dummy_name'), [('3', 'J2M___'), ('4', 'Dummy-TWIN')])
def test_packaged_dummy_turn_load_participates_in_joint_energy_balance(
        family, dummy_name):
    pybada = pytest.importorskip('pyBADA')
    from pyBADA import atmosphere as atm
    from pyBADA import constants

    data_path = Path(pybada.__file__).parent / 'aircraft' / f'BADA{family}' / 'DUMMY'
    version = '3.15' if family == '3' else '4.2'
    model, resolution = ModelStore(family, str(data_path), version=version).resolve('A320')
    assert resolution.resolved == dummy_name
    state = dict(h=3048.0, tas=148.526, mass=64791.0,
                 temperature=268.338, pressure=69676.8, schedule='ICAO',
                 phase='Climb', requested_acceleration=2.0,
                 requested_vertical_rate=8.0)
    straight = EnergyResult(**model.bluesky_energy(**state)).validate()
    bank = 35.0
    load = 1.0 / np.cos(np.radians(bank))
    turning = EnergyResult(**model.bluesky_energy(
        **state, propulsion_bank_angle=bank, load_factor=load)).validate()
    assert turning.drag > straight.drag
    assert turning.load_factor == pytest.approx(load)
    delta_temp = atm.ISATemperatureDeviation(
        temperature=state['temperature'], pressureAltitude=state['h'])
    temperature_factor = (state['temperature'] - delta_temp) / state['temperature']
    specific_power = ((turning.thrust - turning.drag) * state['tas'] /
                      state['mass'])
    allocated_power = (
        state['tas'] * turning.applied_acceleration +
        constants.g * turning.applied_vertical_rate / temperature_factor)
    assert allocated_power == pytest.approx(specific_power, rel=1e-10, abs=1e-10)
    assert allocated_power < (
        (straight.thrust - straight.drag) * state['tas'] / state['mass'])
