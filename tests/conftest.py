import json
from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption('--run-manifest', action='store', default='',
                     help='Local research-run JSON for licensed/network integration tests')


@pytest.fixture
def run_manifest(request):
    value = request.config.getoption('--run-manifest')
    if not value:
        pytest.skip('requires --run-manifest')
    return json.loads(Path(value).read_text(encoding='utf-8'))
