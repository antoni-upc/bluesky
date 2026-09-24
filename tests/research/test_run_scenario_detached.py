from tests.research.run_scenario_detached import unplanned_hold


def test_scenario_and_abort_holds_are_planned():
    assert unplanned_hold([
        'TEST ACTION Commanding +10000 fpm.',
        'PYBADATEM ABORTED ROC_MAX for VABT',
        'Run validate_envelope_run.py on pybada-envelope-abort.csv.']) is None


def test_strict_evaluation_failure_hold_is_unplanned():
    failure = ('PYBADATEM strict evaluation failure: Unbounded TEM output; simulation held. '
               'If recording, use RECORDRESEARCH STOP to finalize partial evidence')
    assert unplanned_hold(['TEST INFO start', failure]) == failure
