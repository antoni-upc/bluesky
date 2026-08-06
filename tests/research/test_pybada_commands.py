from types import SimpleNamespace

import pytest

import bluesky as bs
from bluesky.plugins.pybada.model import ModelUnavailable, Resolution
from bluesky.plugins.pybada_tem import _parse_dynamics_mode, mass, perfmodel, perfstatus
from bluesky.stack.cmdparser import CommandRejected


def test_kinematic_mode_name():
    assert _parse_dynamics_mode('KINEMATIC') == 0


def test_tem_mode_name():
    assert _parse_dynamics_mode('TEM') == 1


def test_unknown_dynamics_mode_is_actionable():
    for value in ('fast', 'DYNAMIC', 'TRUE', '1', 1, 'DYNMODE'):
        with pytest.raises(ValueError, match='KINEMATIC or TEM'):
            _parse_dynamics_mode(value)


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
