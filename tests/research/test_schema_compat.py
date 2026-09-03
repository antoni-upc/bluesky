import pytest

from tests.research.schema_compat import (
    ENERGY_SCHEMAS,
    ENVELOPE_SCHEMAS,
    HORIZONTAL_DYNAMICS_SCHEMAS,
    PROPULSION_LOAD_SCHEMAS,
    compatible_schemas,
    require_schema,
)


def test_historical_schema_capability_ranges_are_explicit():
    assert ENVELOPE_SCHEMAS == (
        "samples-v7", "samples-v8", "samples-v9", "samples-v10")
    assert HORIZONTAL_DYNAMICS_SCHEMAS == (
        "samples-v8", "samples-v9", "samples-v10")
    assert ENERGY_SCHEMAS == ("samples-v9", "samples-v10")
    assert PROPULSION_LOAD_SCHEMAS == ("samples-v10",)


@pytest.mark.parametrize("minimum,accepted", [
    ("samples-v7", "samples-v7"),
    ("samples-v7", "samples-v10"),
    ("samples-v8", "samples-v9"),
    ("samples-v9", "samples-v10"),
    ("samples-v10", "samples-v10"),
])
def test_require_schema_accepts_semantically_additive_versions(minimum, accepted):
    errors = []
    assert require_schema({"schema_version": accepted}, minimum, errors)
    assert errors == []


@pytest.mark.parametrize("minimum,rejected", [
    ("samples-v8", "samples-v7"),
    ("samples-v9", "samples-v8"),
    ("samples-v10", "samples-v9"),
    ("samples-v7", "samples-v11"),
    ("samples-v7", None),
])
def test_require_schema_rejects_older_unknown_and_missing_versions(minimum, rejected):
    errors = []
    assert not require_schema({"schema_version": rejected}, minimum, errors)
    assert repr(rejected) in errors[0]
    assert "requires samples-" in errors[0]


def test_unknown_minimum_is_a_programmer_error():
    with pytest.raises(ValueError, match="Unknown minimum"):
        compatible_schemas("samples-v6")
