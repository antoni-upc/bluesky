from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from bluesky.plugins.pybada.model import EnergyResult, ModelStore


@pytest.mark.licensed_bada
def test_manifest_declares_available_licensed_bada(run_manifest):
    config = run_manifest['configuration']['performance']
    if config['provider'] != 'PYBADA':
        pytest.skip('Run manifest does not select PyBADA')
    resource = run_manifest['external_resources'][config['dataset_id']]
    path = Path(resource['path']).expanduser()
    assert resource['kind'] == 'licensed-bada'
    assert path.is_dir(), f'Licensed BADA directory is unavailable: {path}'
    family = str(config['family'])
    assert config.get('version'), f'BADA {family} version is required'
    assert config.get('aircraft'), f'BADA {family} aircraft list is required'
    if family == '3':
        assert any(path.glob('*.OPF')), 'BADA 3 directory has no OPF files'
    else:
        assert any(child.is_dir() for child in path.iterdir()), 'BADA 4 directory has no model folders'


@pytest.mark.licensed_bada
def test_manifest_licensed_models_resolve_and_evaluate_all_phases(run_manifest):
    config = run_manifest['configuration']['performance']
    if config['provider'] != 'PYBADA':
        pytest.skip('Run manifest does not select PyBADA')
    family = str(config['family'])
    version = str(config['version'])
    phases = config.get('phases', ['Climb', 'Cruise', 'Descent'])
    assert config.get('strict') is True
    resource = run_manifest['external_resources'][config['dataset_id']]
    store = ModelStore(family, resource['path'], version=version,
                       aliases=config.get('aircraft_aliases', {}), strict=True)
    for requested in config['aircraft']:
        model, resolution = store.resolve(requested)
        assert resolution.method in ('exact', 'alias', 'bada3-code')
        assert not resolution.dummy
        for phase in phases:
            result = EnergyResult(**model.bluesky_energy(
                h=3000.0, tas=180.0, mass=60000.0,
                temperature=268.65, pressure=70108.5,
                phase=phase, schedule=config.get('speed_schedule', 'ICAO'))).validate()
            assert np.isfinite(tuple(result.__dict__.values())).all()
            assert result.drag > 0.0
            if phase == 'Climb':
                assert result.rocd > 0.0
            elif phase == 'Cruise':
                assert result.thrust == pytest.approx(result.drag)
                assert result.rocd == pytest.approx(0.0)
            else:
                assert result.rocd < 0.0


@pytest.mark.external_weather
def test_manifest_declares_reproducible_weather_request(run_manifest):
    config = run_manifest['configuration']['atmosphere']
    if config['provider'] == 'ISA':
        pytest.skip('Run manifest does not select NWP meteorology')
    assert config['provider'] in ('ERA5', 'GFS')
    assert len(config['bounds']) == 4
    assert run_manifest['experiment']['simulation_utc']
    assert config.get('strict') is True


@pytest.mark.external_weather
def test_manifest_gfs_grib_is_readable_and_valid(run_manifest):
    config = run_manifest['configuration']['atmosphere']
    if config['provider'] != 'GFS':
        pytest.skip('Run manifest does not select GFS')
    from bluesky.plugins.windgfs import WindGFS

    path = Path(run_manifest['external_resources'][config['dataset_id']]['path']).expanduser()
    assert path.is_file(), f'GFS GRIB is unavailable: {path}'
    slot = datetime.fromisoformat(run_manifest['experiment']['simulation_utc'])
    WindGFS._validate(path)
    cube = object.__new__(WindGFS)._read(path, slot)
    assert cube.source == 'GFS'
    assert cube.dataset_time == slot.isoformat()


@pytest.mark.external_weather
def test_manifest_era5_netcdf_is_readable_and_valid(run_manifest):
    config = run_manifest['configuration']['atmosphere']
    if config['provider'] != 'ERA5':
        pytest.skip('Run manifest does not select ERA5')
    from bluesky.plugins.windecmwf import WindECMWF

    path = Path(run_manifest['external_resources'][config['dataset_id']]['path']).expanduser()
    assert path.is_file(), f'ERA5 NetCDF is unavailable: {path}'
    slot = datetime.fromisoformat(run_manifest['experiment']['simulation_utc'])
    cube = object.__new__(WindECMWF)._read(path, slot)
    assert cube.source == 'ERA5'
    assert cube.dataset_time == slot.isoformat()
