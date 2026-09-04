from types import SimpleNamespace

import pytest

import bluesky as bs
from bluesky.plugins.pybada.model import ModelUnavailable, Resolution
from bluesky.plugins.pybada.envelope import (FlightBounds, LateralBounds,
                                              MassBounds, VerticalBounds)
from bluesky.plugins.pybada_tem import (badaconfig, _parse_dynamics_mode, init_plugin,
                                        mass, perfmodel, perfstatus)
from bluesky.stack.cmdparser import CommandRejected


def test_kinematic_mode_name():
    assert _parse_dynamics_mode('KINEMATIC') == 0


def test_tem_mode_name():
    assert _parse_dynamics_mode('TEM') == 1


def test_plugin_load_without_traffic_defers_default_family_activation(monkeypatch):
    calls = []

    class FakePerformance:
        store = None
        strict = True

        def activate(self):
            calls.append('activate')

        @classmethod
        def select(cls, instance):
            calls.append(('select', instance))

    monkeypatch.setattr('bluesky.plugins.pybada_tem.PyBadaTEM', FakePerformance)
    monkeypatch.setattr(bs, 'traf', SimpleNamespace(id=[]))
    monkeypatch.setattr('bluesky.plugins.pybada_tem.stack.echo', calls.append)
    result = init_plugin()
    assert result['plugin_name'] == 'PYBADATEM'
    assert 'activate' not in calls
    assert any('no dataset selected' in item for item in calls if isinstance(item, str))


def test_unknown_dynamics_mode_is_actionable():
    for value in ('fast', 'DYNAMIC', 'TRUE', '1', 1, 'DYNMODE'):
        with pytest.raises(ValueError, match='KINEMATIC or TEM'):
            _parse_dynamics_mode(value)


def test_badaconfig_is_per_aircraft_and_uses_named_modes(monkeypatch):
    traffic = SimpleNamespace(id=['A1', 'A2'],
                              id2idx=lambda acid: {'A1': 0, 'A2': 1}.get(acid, -1))
    perf = SimpleNamespace(bada_configuration_mode=['PYBADA', 'PYBADA'])

    def configure(idx, mode):
        perf.bada_configuration_mode[idx] = mode.value
        return True, ''

    perf.configure_bada_configuration = configure
    monkeypatch.setattr(bs, 'traf', traffic)
    monkeypatch.setattr('bluesky.plugins.pybada_tem.PyBadaTEM.implinstance', lambda: perf)
    assert badaconfig('A1', 'CRUISE')[0]
    assert perf.bada_configuration_mode == ['CRUISE', 'PYBADA']
    assert badaconfig('A1') == (True, 'A1: CRUISE')
    success, message = badaconfig('A2', 'MANAGED')
    assert not success
    assert 'CRUISE or PYBADA' in message


def test_perfmodel_failure_is_a_normal_command_error(monkeypatch):
    def reject(model):
        raise ModelUnavailable(f'No exact BADA 3 model for A320-232')

    perf = SimpleNamespace(family='4', version='4.2', activate=reject)
    monkeypatch.setattr('bluesky.plugins.pybada_tem.PyBadaTEM.implinstance', lambda: perf)
    success, message = perfmodel('BADA3')
    assert not success
    assert message.startswith('PERFMODEL unchanged:')
    assert 'A320-232' in message


def test_perfstatus_reports_current_mass(monkeypatch):
    traffic = SimpleNamespace(id=['B42'], id2idx=lambda acid: 0 if acid == 'B42' else -1)
    perf = SimpleNamespace(
        family='4', version='4.2',
        resolutions=[Resolution('A320-232', 'A320-232', 'exact', False)],
        dyn_mode=[1], mass=[61234.5], invalid=[False], failure_count=[0])
    monkeypatch.setattr(bs, 'traf', traffic)
    monkeypatch.setattr('bluesky.plugins.pybada_tem.PyBadaTEM.implinstance', lambda: perf)
    success, message = perfstatus('B42')
    assert success
    assert 'mass=61234.5 kg' in message


def test_perfstatus_reports_bada4_lateral_configuration_observation(monkeypatch):
    traffic = SimpleNamespace(id=['B42'], id2idx=lambda acid: 0,
                              alt=[3048.0], vs=[-5.0])
    perf = SimpleNamespace(
        family='4', version='4.2',
        resolutions=[Resolution('A320-232', 'A320-232', 'exact', False)],
        dyn_mode=[0], mass=[61234.5], invalid=[False], failure_count=[0],
        bounds=lambda idx: MassBounds(42_000.0, 78_000.0),
        flight_bounds=lambda idx: FlightBounds('AP', 100.0, 200.0, 0.3,
                                                0.78, 12_000.0),
        vertical_bounds=lambda idx: VerticalBounds(-8.0, 6.0),
        lateral_bounds=lambda idx: LateralBounds(
            'AP', 0.0, 2.0, 60.0, high_lift_id=1.0,
            landing_gear='LGUP', minimum_limit_name='nf3',
            maximum_limit_name='nf1'),
        effective_bank_angle=lambda idx: 0.0)
    monkeypatch.setattr(bs, 'traf', traffic)
    monkeypatch.setattr('bluesky.plugins.pybada_tem.PyBadaTEM.implinstance', lambda: perf)
    success, message = perfstatus('B42')
    assert success
    assert 'config=AP  HLid=1  gear=LGUP' in message
    assert 'DLM=nf3/nf1  load=0.00..2.00' in message
    assert 'alt=3048.0 m (10000 ft/FL100)' in message
    assert 'alt_max=12000.0 m (39370 ft/FL394)' in message

    success, current = perfstatus('B42', 'CURRENT')
    assert success and 'CURRENT' in current and 'BOUNDS' not in current
    success, bounds = perfstatus('B42', 'MAXS')
    assert success and 'BOUNDS' in bounds and 'CURRENT' not in bounds
    assert perfstatus('B42', 'unsupported')[0] is False


def test_perfstatus_labels_bada3_lateral_source_as_gpf(monkeypatch):
    traffic = SimpleNamespace(id=['B3'], id2idx=lambda acid: 0,
                              alt=[3048.0], vs=[0.0])
    perf = SimpleNamespace(
        family='3', version='3.15',
        resolutions=[Resolution('A320', 'A320__', 'bada3-code', False)],
        dyn_mode=[0], mass=[64000.0], invalid=[False], failure_count=[0],
        bounds=lambda idx: MassBounds(40000.0, 80000.0),
        flight_bounds=lambda idx: FlightBounds('CR', 100.0, 200.0, 0.3,
                                                0.82, 11800.0),
        vertical_bounds=lambda idx: VerticalBounds(-8.0, 6.0),
        lateral_bounds=lambda idx: LateralBounds(
            'CR', None, 2.0 ** 0.5, 45.0,
            maximum_limit_name='derived'),
        effective_bank_angle=lambda idx: 0.0)
    monkeypatch.setattr(bs, 'traf', traffic)
    monkeypatch.setattr('bluesky.plugins.pybada_tem.PyBadaTEM.implinstance', lambda: perf)
    success, message = perfstatus('B3', 'BOUNDS')
    assert success
    assert 'Lateral: source=GPF-civilian/derived' in message
    assert 'DLM=' not in message


def test_mass_fundamental_rejection_names_aircraft_and_preserved_state(monkeypatch):
    traffic = SimpleNamespace(id=['AC1'])
    perf = SimpleNamespace(mass=[61_000.0])
    monkeypatch.setattr(bs, 'traf', traffic)
    monkeypatch.setattr('bluesky.plugins.pybada_tem.PyBadaTEM.implinstance', lambda: perf)
    success, message = mass(0, 0.0)
    assert not success
    assert isinstance(message, CommandRejected)
    assert 'AC1:' in message
    assert 'finite and positive' in message
    assert 'preserved=61000.0 kg' in message
