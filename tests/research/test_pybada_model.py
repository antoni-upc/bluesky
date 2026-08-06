from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from bluesky.plugins.pybada.model import (BadaModelAdapter, EnergyResult,
                                          EvaluationError, ModelStore, ModelUnavailable)
from bluesky.plugins.pybada.performance import PyBadaTEM


def test_energy_result_rejects_missing_or_negative_physics():
    EnergyResult(10, 12, 8, 0.2, 1, 2, 0.1).validate()
    with pytest.raises(EvaluationError):
        EnergyResult(10, 12, 8, -0.2, 1, 2, 0.1).validate()
    with pytest.raises(EvaluationError):
        EnergyResult(float('nan'), 12, 8, 0.2, 1, 2, 0.1).validate()


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


def test_bada3_lateral_adapter_uses_phase_bank_limit():
    aircraft = SimpleNamespace(flightEnvelope=FakeBada3Envelope())
    result = BadaModelAdapter(aircraft, '3').bluesky_lateral_envelope(
        configuration='CR', phase='Climb')
    assert result['maximum_bank_angle_deg'] == 30.0
    assert result['maximum_load_factor'] == pytest.approx(
        1.0 / np.cos(np.radians(30.0)))
    assert result['minimum_load_factor'] is None


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
    assert clean['maximum_bank_angle_deg'] == pytest.approx(
        np.degrees(np.arccos(1.0 / 2.5)))
    assert (high_lift['minimum_load_factor'], high_lift['maximum_load_factor']) == (0.0, 2.0)
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
