import pytest

from tests.research.schema_compat import SCHEMA_VERSION, require_schema


def test_active_schema_is_exactly_v11():
    assert SCHEMA_VERSION == "samples-v11"


def test_require_schema_accepts_v11():
    errors = []
    assert require_schema({"schema_version": "samples-v11"}, errors)
    assert errors == []


@pytest.mark.parametrize("rejected", [
    "samples-v7", "samples-v8", "samples-v9", "samples-v10",
    "samples-v12", None,
])
def test_require_schema_rejects_every_non_v11_version(rejected):
    errors = []
    assert not require_schema({"schema_version": rejected}, errors)
    assert repr(rejected) in errors[0]
    assert "is not samples-v11" in errors[0]
