"""Lifecycle and model-family isolation gates for PYBADATEM."""

from types import SimpleNamespace

import numpy as np
import pytest

from bluesky.plugins.pybada.model import ModelUnavailable, Resolution
from bluesky.plugins.pybada.performance import PyBadaTEM
from bluesky.traffic.performance.perfbase import PerfBase


LIST_FIELDS = (
    'models', 'resolutions', 'envelope_checks', 'envelope_failed_checks',
    'envelope_mass_failed_checks', 'envelope_state_failed_checks',
    'envelope_guidance_failed_checks',
)


def lifecycle_stub(count=3):
    perf = object.__new__(PyBadaTEM)
    for field in LIST_FIELDS:
        setattr(perf, field, [f'{field}-{index}' for index in range(count)])
    return perf


def test_multi_delete_keeps_all_python_lifecycle_arrays_aligned(monkeypatch):
    perf = lifecycle_stub(4)
    base_calls = []
    monkeypatch.setattr(PerfBase, 'delete', lambda self, idx: base_calls.append(idx))

    perf.delete(np.array([1, 3]))

    for field in LIST_FIELDS:
        assert getattr(perf, field) == [f'{field}-0', f'{field}-2']
    assert len({len(getattr(perf, field)) for field in LIST_FIELDS}) == 1
    assert len(base_calls) == 1
    assert np.array_equal(base_calls[0], np.array([1, 3]))


def test_reset_clears_all_python_lifecycle_arrays(monkeypatch):
    perf = lifecycle_stub()
    base_calls = []
    monkeypatch.setattr(PerfBase, 'reset', lambda self: base_calls.append(True))

    perf.reset()

    assert all(getattr(perf, field) == [] for field in LIST_FIELDS)
    assert base_calls == [True]


def test_compatible_bada3_reactivation_is_atomic_and_preserves_aircraft_state(monkeypatch):
    old_models = [object(), object()]
    old_resolutions = [object(), object()]
    perf = object.__new__(PyBadaTEM)
    perf.family = '3'
    perf.version = '3.15'
    perf.strict = True
    perf.store = object()
    perf.models = old_models.copy()
    perf.resolutions = old_resolutions.copy()
    perf.envelope_checks = []
    perf.envelope_policy = np.array(['REPORT', 'ENFORCE'])
    perf.dyn_mode = np.array([0, 1])
    perf.bada_configuration_mode = np.array(['PYBADA', 'CRUISE'])
    perf.mass = np.array([61_000.0, 62_000.0])

    models = {'A320': object(), 'A319': object()}

    class CompatibleStore:
        version = '3.15'

        def __init__(self, *args, **kwargs):
            pass

        def resolve(self, actype):
            return models[actype], Resolution(actype, f'{actype}__', 'bada3-code', False)

    monkeypatch.setattr('bluesky.plugins.pybada.performance.ModelStore', CompatibleStore)
    monkeypatch.setattr('bluesky.plugins.pybada.performance.bs.traf',
                        SimpleNamespace(type=['A320', 'A319']))
    monkeypatch.setattr('bluesky.plugins.pybada.performance.bs.settings.pybada3_data_path',
                        '/configured/bada3')
    monkeypatch.setattr('bluesky.plugins.pybada.performance.bs.settings.pybada3_version',
                        '3.15')

    models_identity = id(perf.models)
    resolutions_identity = id(perf.resolutions)
    preserved = (perf.envelope_policy.copy(), perf.dyn_mode.copy(),
                 perf.bada_configuration_mode.copy(), perf.mass.copy())
    perf.activate('3')

    assert perf.family == '3' and perf.version == '3.15'
    assert perf.models == [models['A320'], models['A319']]
    assert [item.resolved for item in perf.resolutions] == ['A320__', 'A319__']
    assert id(perf.models) == models_identity
    assert id(perf.resolutions) == resolutions_identity
    for actual, expected in zip((perf.envelope_policy, perf.dyn_mode,
                                 perf.bada_configuration_mode, perf.mass), preserved):
        assert np.array_equal(actual, expected)


def test_incompatible_family_switch_preserves_complete_bada3_state(monkeypatch):
    perf = object.__new__(PyBadaTEM)
    perf.family = '3'
    perf.version = '3.15'
    perf.strict = True
    perf.store = object()
    perf.models = [object(), object()]
    perf.resolutions = [object(), object()]
    perf.envelope_checks = [('MASS_MIN',), ('ROC_MAX',)]
    perf.envelope_policy = np.array(['REPORT', 'ENFORCE'])
    perf.envelope_profile = np.array(['CUSTOM', 'CUSTOM'])
    perf.dyn_mode = np.array([0, 1])
    perf.bada_configuration_mode = np.array(['PYBADA', 'CRUISE'])
    perf.mass = np.array([61_000.0, 62_000.0])

    class IncompatibleStore:
        version = '4.2'

        def __init__(self, *args, **kwargs):
            pass

        def resolve(self, actype):
            if actype == 'A319':
                raise ModelUnavailable('No BADA 4 model for A319')
            return object(), Resolution(actype, actype, 'exact', False)

    monkeypatch.setattr('bluesky.plugins.pybada.performance.ModelStore', IncompatibleStore)
    monkeypatch.setattr('bluesky.plugins.pybada.performance.bs.traf',
                        SimpleNamespace(type=['A320', 'A319']))
    monkeypatch.setattr('bluesky.plugins.pybada.performance.bs.settings.pybada4_data_path',
                        '/configured/bada4')
    monkeypatch.setattr('bluesky.plugins.pybada.performance.bs.settings.pybada4_version',
                        '4.2')

    before = {
        'family': perf.family, 'version': perf.version, 'store': perf.store,
        'models': perf.models.copy(), 'resolutions': perf.resolutions.copy(),
        'checks': perf.envelope_checks.copy(), 'policy': perf.envelope_policy.copy(),
        'profile': perf.envelope_profile.copy(), 'dynamics': perf.dyn_mode.copy(),
        'configuration': perf.bada_configuration_mode.copy(), 'mass': perf.mass.copy(),
    }
    with pytest.raises(ModelUnavailable, match='A319'):
        perf.activate('4')

    assert perf.family == before['family'] and perf.version == before['version']
    assert perf.store is before['store']
    assert perf.models == before['models'] and perf.resolutions == before['resolutions']
    assert perf.envelope_checks == before['checks']
    for field, expected in (
            ('envelope_policy', before['policy']), ('envelope_profile', before['profile']),
            ('dyn_mode', before['dynamics']),
            ('bada_configuration_mode', before['configuration']), ('mass', before['mass'])):
        assert np.array_equal(getattr(perf, field), expected)
