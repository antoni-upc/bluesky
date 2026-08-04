import csv
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

import bluesky as bs
from bluesky.plugins.meteo.recorder import StreamingRecorder
from bluesky.plugins.pybada.model import Resolution


@pytest.mark.smoke
def test_recorder_streams_and_resets_without_retaining_rows(tmp_path, monkeypatch):
    values = np.array([1.0])
    traffic = SimpleNamespace(
        id=['TST1'], type=['A320'], lat=values, lon=values, alt=values,
        pressure_alt=values, tas=values, cas=values, M=values, vs=values,
        hdg=values, trk=values, Temp=np.array([280.0]), p=np.array([90000.0]),
        rho=np.array([90000.0 / (287.05287 * 280.0)]), windnorth=values,
        windeast=values, atmos_source=['SYNTHETIC'], atmos_valid=np.array([True]),
        atmos_dataset_time=['2026-01-01T00:00:00+00:00'], atmos_fallback_reason=[''],
        perf=SimpleNamespace(
            family='4', thrust=np.array([np.nan]), rated_thrust=np.array([2.0]), drag=values,
            fuelflow=values, mass=np.array([60000.0]), dyn_mode=np.array([1]),
            invalid=np.array([False]), failure_count=np.array([2]),
            resolutions=[Resolution('A320', 'DUMMY-TWIN', 'dummy', True)]))
    monkeypatch.setattr(bs, 'traf', traffic)
    monkeypatch.setattr(bs, 'sim', SimpleNamespace(
        simt=10.0, utc=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    recorder = StreamingRecorder()
    path = tmp_path / 'samples.csv'
    recorder.start(path)
    recorder.sample()
    assert recorder.rows == 1
    csv_path, metadata_path = recorder.stop()
    with csv_path.open(newline='') as stream:
        row = next(csv.DictReader(stream))
    assert row['thrust_n'] == ''
    assert row['rated_thrust_n'] == '2.0'
    assert row['sample_interval_s'] == ''
    assert row['atmosphere_source'] == 'SYNTHETIC'
    assert row['dynamics_mode'] == 'TEM'
    assert row['performance_aircraft'] == 'DUMMY-TWIN'
    assert row['performance_resolution'] == 'dummy'
    assert row['performance_dummy'] == 'True'
    assert row['performance_valid'] == 'True'
    assert row['performance_miss_count'] == '2'
    metadata = json.loads(metadata_path.read_text())
    assert metadata['rows'] == 1
    assert metadata['sample_intervals_s'] == []
    assert not hasattr(recorder, '_rows')
    recorder.reset()
    assert recorder.rows == 0
