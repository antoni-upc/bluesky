"""Portable contract for the timestep-convergence analysis and study tool."""

import csv

import pytest

from tests.research.convergence import analyse, dt_label
from tests.research.run_convergence_study import variants

DTS = (0.10, 0.05, 0.025)
FIELDS = ('geometric_alt_m', 'tas_m_s', 'mass_kg', 'vertical_speed_m_s', 'speed_capture',
          'sim_time_s', 'acid')


def write_run(path, dt, *, order=1.0, bias=0.0, capture_at=None, altitude_offset=0.0):
    """One aircraft on a 0.1 s grid; its error behaves like dt**order."""
    rows = []
    for step in range(301):
        t = round(step * 0.1, 6)
        error = dt ** order * t + bias
        rows.append({'acid': 'A1', 'sim_time_s': t,
                     'geometric_alt_m': 1000.0 + 5.0 * t + altitude_offset,
                     'tas_m_s': 200.0 + 0.2 * t + error,
                     'mass_kg': 60_000.0 - 0.5 * t,
                     'vertical_speed_m_s': 5.0,
                     'speed_capture': capture_at is not None and t >= capture_at})
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def runs(tmp_path, **kwargs):
    capture = kwargs.pop('capture', None)
    return {dt: write_run(tmp_path / f'{dt_label(dt)}.csv', dt,
                          capture_at=None if capture is None else capture(dt), **kwargs)
            for dt in DTS}


def test_first_order_errors_pass_with_unit_order(tmp_path):
    errors, report = analyse(runs(tmp_path), fields=('tas_m_s', 'geometric_alt_m'))
    assert errors == []
    tas = report['aircraft']['A1']['fields']['tas_m_s']
    assert tas['observed_order'] == pytest.approx(1.0)
    assert tas['whole_run_reduction'] == pytest.approx(3.0)
    assert report['aircraft']['A1']['fields']['geometric_alt_m']['status'] == 'resolved'


def test_errors_that_do_not_shrink_fail(tmp_path):
    errors, _ = analyse(runs(tmp_path, order=0.1), fields=('tas_m_s',))
    assert any('observed order' in error for error in errors)
    assert any('whole-run error shrinks only' in error for error in errors)


def test_exact_fields_must_agree_at_every_timestep(tmp_path):
    paths = runs(tmp_path)
    assert analyse(paths, fields=('geometric_alt_m',),
                   exact={'A1': ('geometric_alt_m',)})[0] == []
    write_run(paths[0.10], 0.10, altitude_offset=0.5)
    errors, _ = analyse(paths, fields=('geometric_alt_m',), exact={'A1': ('geometric_alt_m',)})
    assert errors and 'expected exact integration' in errors[0]


def test_event_times_must_converge_within_a_few_steps(tmp_path):
    good = runs(tmp_path, capture=lambda dt: 12.0 + dt)
    assert analyse(good, fields=('tas_m_s',))[0] == []
    bad = runs(tmp_path, capture=lambda dt: 12.0 + 20 * dt)
    errors, _ = analyse(bad, fields=('tas_m_s',))
    assert any('speed_capture #1' in error for error in errors)


def test_order_is_measured_before_the_first_event(tmp_path):
    paths = runs(tmp_path, capture=lambda dt: 10.0)
    _, report = analyse(paths, fields=('tas_m_s',))
    entry = report['aircraft']['A1']
    assert entry['smooth_samples'] < entry['common_samples']
    assert entry['fields']['tas_m_s']['observed_order'] == pytest.approx(1.0)


def test_observable_error_at_the_production_step_is_extrapolated(tmp_path):
    paths = runs(tmp_path)
    errors, report = analyse(paths, fields=('tas_m_s',), observable='final:tas_m_s',
                             production_dt=0.05, tolerance=2.0)
    observable = report['aircraft']['A1']['observable']
    # Final TAS error is 30 * dt, so the limit is 206 and the 0.05 s error 1.5.
    assert observable['extrapolated'] == pytest.approx(206.0)
    assert observable['estimated_error_at_production_dt'] == pytest.approx(1.5)
    assert errors == []
    errors, _ = analyse(paths, fields=('tas_m_s',), observable='final:tas_m_s',
                        production_dt=0.05, tolerance=1.0)
    assert any('exceeds tolerance' in error for error in errors)


def test_timesteps_need_one_constant_ratio(tmp_path):
    paths = {dt: write_run(tmp_path / f'{dt}.csv', dt) for dt in (0.10, 0.05, 0.02)}
    with pytest.raises(ValueError, match='constant ratio'):
        analyse(paths)


def test_variants_replace_dt_and_rename_the_recorder_output(tmp_path, monkeypatch):
    import tests.research.run_convergence_study as study
    monkeypatch.setattr(study, 'ROOT', tmp_path)
    source = tmp_path / 'scenario' / 'research' / 'demo.scn'
    source.parent.mkdir(parents=True)
    source.write_text('00:00:00.40>DT 0.02\n00:00:01.00>RECORDRESEARCH INTERVAL 0.10\n'
                      '00:00:02.00>RECORDRESEARCH START demo.csv\n', encoding='utf-8')
    result = variants('research/demo')
    assert sorted(result) == sorted(DTS)
    path, csv_path = result[0.025]
    text = path.read_text(encoding='utf-8')
    assert '00:00:00.00>DT 0.025' in text and 'DT 0.02\n' not in text
    assert 'RECORDRESEARCH START demo-dt025.csv' in text
    assert csv_path == tmp_path / 'output' / 'demo-dt025.csv'
    source.write_text('00:00:01.00>RECORDRESEARCH INTERVAL 0.03\n'
                      '00:00:02.00>RECORDRESEARCH START demo.csv\n', encoding='utf-8')
    with pytest.raises(ValueError, match='not a multiple'):
        variants('research/demo')
