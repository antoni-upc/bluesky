import csv

import pytest

from tests.research import run_integration_profile as runner


def test_validate_recorder_accepts_expected_openap_weather_rows(tmp_path):
    path = tmp_path / "samples.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=(
            "atmosphere_source", "atmosphere_valid", "fallback_reason",
            "performance_model",
        ))
        writer.writeheader()
        writer.writerow({"atmosphere_source": "ERA5", "atmosphere_valid": "True",
                         "fallback_reason": "", "performance_model": "OpenAP"})

    rows = runner.validate_recorder(path, "ERA5", 1)

    assert len(rows) == 1


@pytest.mark.parametrize("field,value", [
    ("atmosphere_source", "ISA"),
    ("atmosphere_valid", "False"),
    ("fallback_reason", "OUTSIDE_DOMAIN"),
    ("performance_model", "PyBadaTEM"),
])
def test_validate_recorder_rejects_wrong_provenance(tmp_path, field, value):
    row = {"atmosphere_source": "GFS", "atmosphere_valid": "True",
           "fallback_reason": "", "performance_model": "OpenAP"}
    row[field] = value
    path = tmp_path / "samples.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=row)
        writer.writeheader()
        writer.writerow(row)

    with pytest.raises(RuntimeError):
        runner.validate_recorder(path, "GFS", 1)


def test_compare_common_samples_requires_exact_values():
    sample = {sample_field: 1.0
              for sample_field in runner.RECORDER_SAMPLE_FIELDS.values()}
    sample["sim_time_s"] = 0.5
    row = {recorder_field: "1.0"
           for recorder_field in runner.RECORDER_SAMPLE_FIELDS}
    row["sim_time_s"] = "0.5"

    runner.compare_common_samples([row], [sample])

    row["temperature_k"] = "1.0001"
    with pytest.raises(RuntimeError, match="temperature_k"):
        runner.compare_common_samples([row], [sample])
