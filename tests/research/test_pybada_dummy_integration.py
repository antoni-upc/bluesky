from pathlib import Path

import numpy as np
import pytest

pybada = pytest.importorskip('pyBADA')

from bluesky.plugins.pybada.model import ModelStore


@pytest.mark.parametrize('family,directory', [('3', 'BADA3'), ('4', 'BADA4')])
def test_packaged_dummy_models_evaluate_all_phases(family, directory):
    data = Path(pybada.__file__).resolve().parent / 'aircraft' / directory / 'DUMMY'
    version = '3.15' if family == '3' else '4.2'
    model, resolution = ModelStore(family, str(data), version=version, strict=False).resolve('A320')
    assert resolution.dummy
    if family == '4':
        assert resolution.resolved.upper() == 'DUMMY-TWIN'
    for phase in ('Climb', 'Cruise', 'Descent'):
        result = model.bluesky_energy(
            h=3000.0, tas=180.0, mass=60000.0, temperature=268.65,
            pressure=70108.5, phase=phase, schedule='ICAO')
        values = np.asarray(tuple(result.values()), dtype=float)
        assert np.all(np.isfinite(values))
        assert result['drag'] > 0.0
        assert result['fuel_flow'] >= 0.0
    assert model.bluesky_energy(
        h=3000.0, tas=180.0, mass=60000.0, temperature=268.65,
        pressure=70108.5, phase='Climb', schedule='ICAO')['rocd'] > 0.0
