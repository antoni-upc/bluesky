import re
from pathlib import Path

import pytest

from tests.research.schema_compat import SCHEMA_VERSION, require_schema


def test_active_schema_is_exactly_v12():
    assert SCHEMA_VERSION == "samples-v12"


def test_require_schema_accepts_v12():
    errors = []
    assert require_schema({"schema_version": "samples-v12"}, errors)
    assert errors == []


@pytest.mark.parametrize("rejected", [
    "samples-v7", "samples-v8", "samples-v9", "samples-v10",
    "samples-v11", None,
])
def test_require_schema_rejects_every_non_v12_version(rejected):
    errors = []
    assert not require_schema({"schema_version": rejected}, errors)
    assert repr(rejected) in errors[0]
    assert "is not samples-v12" in errors[0]


def test_recorder_writes_the_schema_validators_accept():
    from bluesky.plugins.recorder import SCHEMA_VERSION as RECORDER_SCHEMA_VERSION
    assert RECORDER_SCHEMA_VERSION == SCHEMA_VERSION


def test_research_tooling_reads_schema_version_from_one_place():
    research = Path(__file__).resolve().parent
    tooling = [path for pattern in ('validate_*.py', 'run_*.py', 'audit_*.py')
               for path in research.glob(pattern)]
    assert tooling
    offenders = [path.name for path in tooling
                 if re.search(r"""['"]samples-v\d+['"]""", path.read_text(encoding='utf-8'))]
    assert offenders == []
