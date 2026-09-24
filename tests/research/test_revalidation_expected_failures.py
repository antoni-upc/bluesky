import pytest

from tests.research import run_pybada_revalidation as runner


def write_log(tmp_path, monkeypatch, text):
    monkeypatch.setattr(runner, 'failure_log', lambda scenario: tmp_path / f'{scenario}.log')
    (tmp_path / 'gate.log').write_text(text, encoding='utf-8')


def test_expected_failure_passes_on_detected_unplanned_hold(tmp_path, monkeypatch):
    write_log(tmp_path, monkeypatch,
              'exit=1\nUNPLANNED HOLD: PYBADATEM strict evaluation failure: '
              'LATR: Unbounded TEM output: ROCD=0.0\n')
    assert 'detected' in runner.validate_expected_failure('gate', 'Unbounded TEM output')


@pytest.mark.parametrize('text', [
    'exit=0\nTEST ERROR Expected strict evaluation failure did not hold the simulation.\n',
    'exit=1\nTraceback (most recent call last):\nRuntimeError: weather\n',
    'exit=1\nUNPLANNED HOLD: PYBADATEM strict evaluation failure: other reason\n',
])
def test_expected_failure_rejects_completion_or_other_failures(tmp_path, monkeypatch, text):
    write_log(tmp_path, monkeypatch, text)
    with pytest.raises(RuntimeError, match='expected an unplanned strict-failure HOLD'):
        runner.validate_expected_failure('gate', 'Unbounded TEM output')


def test_expected_failure_gates_are_not_ordinary_matrix_entries():
    ordinary = {scenario for scenario, _, _ in runner.matrix()}
    assert not ordinary.intersection(runner.EXPECTED_FAILURES)
    for scenario in runner.EXPECTED_FAILURES:
        assert (runner.ROOT / 'scenario/research' / f'{scenario}.scn').is_file()
