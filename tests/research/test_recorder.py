import csv
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

import bluesky as bs
from bluesky.plugins.meteo.recorder import StreamingRecorder
from bluesky.plugins.research_recorder import atmosstatus
from bluesky.plugins.pybada.model import Resolution
from bluesky.plugins.pybada.envelope import QualityEvent
from bluesky.plugins.pybada.envelope import LateralBounds


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
            family='4', version='4.2', thrust=np.array([np.nan]), rated_thrust=np.array([2.0]), drag=values,
            fuelflow=values, mass=np.array([60000.0]), dyn_mode=np.array([1]),
            bada_configuration_mode=np.array(['CRUISE']),
            invalid=np.array([False]), failure_count=np.array([2]),
            resolutions=[Resolution('A320', 'DUMMY-TWIN', 'dummy', True)],
            lateral_bounds=lambda idx: LateralBounds(
                'CR', -1.0, 2.5, 66.4218, high_lift_id=0.0,
                landing_gear='LGUP', minimum_limit_name='n3',
                maximum_limit_name='n1')))
    monkeypatch.setattr(bs, 'traf', traffic)
    monkeypatch.setattr(bs, 'sim', SimpleNamespace(
        simt=10.0, utc=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    monkeypatch.setattr('bluesky.plugins.meteo.recorder.stack.get_scenname',
                        lambda: 'recorder-test')
    recorder = StreamingRecorder()
    path = tmp_path / 'samples.csv'
    recorder.start(path)
    recorder.sample()
    traffic.atmos_source[0] = 'ERA5'
    traffic.atmos_dataset_time[0] = '2026-01-01T01:00:00+00:00'
    recorder.sample()
    assert recorder.rows == 2
    csv_path, metadata_path = recorder.stop()
    with csv_path.open(newline='') as stream:
        row = next(csv.DictReader(stream))
    assert row['thrust_n'] == ''
    assert row['rated_thrust_n'] == '2.0'
    assert row['sample_interval_s'] == ''
    assert row['atmosphere_source'] == 'SYNTHETIC'
    assert row['dynamics_mode'] == 'TEM'
    assert row['performance_aircraft'] == 'DUMMY-TWIN'
    assert row['performance_dataset_version'] == '4.2'
    assert row['performance_resolution'] == 'dummy'
    assert row['performance_dummy'] == 'True'
    assert row['performance_valid'] == 'True'
    assert row['performance_miss_count'] == '2'
    assert row['schema_version'] == 'samples-v9'
    for field in ('bank_angle_deg', 'load_factor', 'minimum_load_factor',
                  'maximum_load_factor', 'maximum_bank_angle_deg'):
        assert field in row
    metadata = json.loads(metadata_path.read_text())
    assert metadata['schema_version'] == 'samples-v9'
    assert metadata['rows'] == 2
    assert metadata['atmosphere_sources'] == ['ERA5', 'SYNTHETIC']
    assert metadata['dataset_times'] == [
        '2026-01-01T00:00:00+00:00', '2026-01-01T01:00:00+00:00']
    assert metadata['sample_intervals_s'] == []
    assert metadata['scenario'] == 'recorder-test'
    effective = metadata['effective_envelope'][0]
    assert effective['configuration_mode'] == 'CRUISE'
    assert effective['configuration'] == 'CR'
    assert effective['high_lift_id'] == 0.0
    assert effective['landing_gear'] == 'LGUP'
    assert effective['minimum_limit_name'] == 'n3'
    assert effective['maximum_limit_name'] == 'n1'
    assert effective['minimum_load_factor'] == -1.0
    assert effective['maximum_load_factor'] == 2.5
    assert effective['maximum_bank_angle_deg'] == 66.4218
    assert not hasattr(recorder, '_rows')
    recorder.reset()
    assert recorder.rows == 0


def test_atmosstatus_exposes_applied_state_and_isa_difference(monkeypatch):
    traffic = SimpleNamespace(
        ntraf=1, id=['WX1'], alt=np.array([3000.0]), pressure_alt=np.array([3100.0]),
        Temp=np.array([270.0]), p=np.array([69000.0]), rho=np.array([0.89]),
        windnorth=np.array([5.0]), windeast=np.array([-2.0]), tas=np.array([180.0]),
        cas=np.array([150.0]), M=np.array([0.55]), gs=np.array([182.0]),
        atmos_source=['ERA5'], atmos_valid=np.array([True]),
        atmos_dataset_time=['2026-07-25T06:00:00'], atmos_fallback_reason=[''],
        id2idx=lambda acid: 0 if acid == 'WX1' else -1)
    monkeypatch.setattr(bs, 'traf', traffic)
    success, message = atmosstatus('WX1')
    assert success
    for value in ('source=ERA5', 'T=270.000 K', 'p=69000.000 Pa',
                  'pressure_alt=3100.0 m', 'wind_north=5.000 m/s', 'ISA'):
        assert value in message


def test_quality_events_are_optional_synchronous_and_summarized(tmp_path, monkeypatch):
    recorder = StreamingRecorder()
    event = QualityEvent('A1', 'PYBADATEM', 'MASS_MAX', 'REPORT',
                         'ACCEPTED', 'CONTINUE', 90_000.0, 90_000.0, 3.0)
    recorder.observe_event(event)
    assert not list(tmp_path.iterdir())
    monkeypatch.setattr(bs, 'traf', SimpleNamespace(id=[], atmos_source=[],
                                                    atmos_dataset_time=[], perf=None))
    monkeypatch.setattr(bs, 'sim', SimpleNamespace(scenname='quality-test'))
    recorder.start(tmp_path / 'run.csv')
    recorder.observe_event(event)
    assert recorder.event_path.read_text().endswith('\n')
    _, metadata_path = recorder.stop()
    metadata = json.loads(metadata_path.read_text())
    assert metadata['event_total'] == 1
    assert metadata['reason_totals'] == {'MASS_MAX': 1}
    assert metadata['quality_status'] == 'DEGRADED'


def test_abort_event_auto_finalizes_evidence_before_return(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, 'traf', SimpleNamespace(id=[], atmos_source=[],
                                                    atmos_dataset_time=[], perf=None))
    monkeypatch.setattr(bs, 'sim', SimpleNamespace(scenname='abort-test'))
    recorder = StreamingRecorder()
    csv_path = tmp_path / 'abort.csv'
    recorder.start(csv_path)
    recorder.observe_event(QualityEvent(
        'A1', 'PYBADATEM', 'MASS_MAX', 'ABORT', 'ABORTED', 'STOP',
        100_000.0, 100_000.0, 4.0))
    assert not recorder.active
    assert csv_path.with_suffix('.events.jsonl').read_text().endswith('\n')
    metadata = json.loads(csv_path.with_suffix('.metadata.json').read_text())
    assert metadata['event_total'] == 1
    assert metadata['reason_totals'] == {'MASS_MAX': 1}
    assert metadata['quality_status'] == 'ABORTED'
