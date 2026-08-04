from pathlib import Path
from types import SimpleNamespace

import pytest

from bluesky.plugins.pybada.model import EnergyResult, EvaluationError, ModelStore, ModelUnavailable
from bluesky.plugins.pybada.performance import PyBadaTEM


def test_energy_result_rejects_missing_or_negative_physics():
    EnergyResult(10, 12, 8, 0.2, 1, 2, 0.1).validate()
    with pytest.raises(EvaluationError):
        EnergyResult(10, 12, 8, -0.2, 1, 2, 0.1).validate()
    with pytest.raises(EvaluationError):
        EnergyResult(float('nan'), 12, 8, 0.2, 1, 2, 0.1).validate()


def test_strict_resolution_only_exact_or_alias(tmp_path):
    (tmp_path / 'A320.OPF').touch()
    store = ModelStore('3', str(tmp_path), strict=True)
    with pytest.raises(ModelUnavailable):
        store.resolve('A32X')


def test_interactive_resolution_is_not_prefix_matching(tmp_path):
    (tmp_path / 'A320.OPF').touch()
    store = ModelStore('3', str(tmp_path), strict=False)
    with pytest.raises(ModelUnavailable):
        store.resolve('A32X')


def test_create_validation_rejects_before_mutating_performance_state(tmp_path):
    (tmp_path / 'A320.OPF').touch()
    perf = object.__new__(PyBadaTEM)
    perf.store = ModelStore('3', str(tmp_path), strict=True)
    perf.models = []
    perf.resolutions = []
    valid, message = perf.validate_create(['A32X'])
    assert not valid
    assert 'A32X' in message
    assert perf.models == []
    assert perf.resolutions == []


@pytest.mark.smoke
@pytest.mark.parametrize(('family', 'dummy_name'), [('3', 'J2M___'), ('4', 'Dummy-TWIN')])
def test_packaged_dummy_uses_equilibrium_cruise_and_rated_phase_thrust(
        family, dummy_name):
    pybada = pytest.importorskip('pyBADA')
    data_path = Path(pybada.__file__).parent / 'aircraft' / f'BADA{family}' / 'DUMMY'
    model, resolution = ModelStore(family, str(data_path)).resolve('A320')
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
