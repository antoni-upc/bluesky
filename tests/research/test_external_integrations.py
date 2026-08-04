from pathlib import Path

import pytest


@pytest.mark.licensed_bada
def test_manifest_declares_available_licensed_bada(run_manifest):
    config = run_manifest['pybada']
    path = Path(config['data_path']).expanduser()
    assert path.is_dir(), f'Licensed BADA directory is unavailable: {path}'
    family = str(config['family'])
    if family == '3':
        assert any(path.glob('*.OPF')), 'BADA 3 directory has no OPF files'
    else:
        assert any(child.is_dir() for child in path.iterdir()), 'BADA 4 directory has no model folders'


@pytest.mark.external_weather
def test_manifest_declares_reproducible_weather_request(run_manifest):
    config = run_manifest['meteorology']
    assert config['source'] in ('ERA5', 'GFS')
    assert len(config['bounds']) == 4
    assert config['simulation_utc']
    assert config.get('strict') is True
