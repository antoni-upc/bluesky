from datetime import datetime
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


@pytest.mark.external_weather
def test_manifest_gfs_grib_is_readable_and_valid(run_manifest):
    config = run_manifest['meteorology']
    if config['source'] != 'GFS':
        pytest.skip('Run manifest does not select GFS')
    from bluesky.plugins.windgfs import WindGFS

    path = Path(config['data_path']).expanduser()
    assert path.is_file(), f'GFS GRIB is unavailable: {path}'
    slot = datetime.fromisoformat(config['simulation_utc'])
    WindGFS._validate(path)
    cube = object.__new__(WindGFS)._read(path, slot)
    assert cube.source == 'GFS'
    assert cube.dataset_time == slot.isoformat()


@pytest.mark.external_weather
def test_manifest_era5_netcdf_is_readable_and_valid(run_manifest):
    config = run_manifest['meteorology']
    if config['source'] != 'ERA5':
        pytest.skip('Run manifest does not select ERA5')
    from bluesky.plugins.windecmwf import WindECMWF

    path = Path(config['data_path']).expanduser()
    assert path.is_file(), f'ERA5 NetCDF is unavailable: {path}'
    slot = datetime.fromisoformat(config['simulation_utc'])
    cube = object.__new__(WindECMWF)._read(path, slot)
    assert cube.source == 'ERA5'
    assert cube.dataset_time == slot.isoformat()
