from tests.research.compare_recorder_noninterference import first_difference


def test_first_difference_accepts_identical_external_samples():
    samples = [{"step": 0, "alt": 1000.0}, {"step": 1, "alt": 1001.0}]
    assert first_difference(samples, samples) is None


def test_first_difference_reports_exact_sample_and_field():
    left = [{"step": 0, "alt": 1000.0}, {"step": 1, "alt": 1001.0}]
    right = [{"step": 0, "alt": 1000.0}, {"step": 1, "alt": 1002.0}]
    assert first_difference(left, right) == "samples[1].alt: 1001.0 != 1002.0"
