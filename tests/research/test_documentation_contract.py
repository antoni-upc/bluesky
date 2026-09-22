from pathlib import Path
import re


DOCS = Path('docs')
RECORDER_DOC = DOCS / 'plugin/recorder/recorder-v11-contract.md'
WEATHER_DOC = DOCS / 'plugin/nwp-meteo/README-weather.md'
PYBADA_DOC = DOCS / 'plugin/pybada-tem/bada-envelope-implementation.md'
ACTIVE_DOCS = tuple(DOCS / name for name in (
    'research-plugins.md', 'current-plugin-architecture.md',
    'recorder-v11-contract.md',
    'bada-envelope-implementation.md', 'reproducibility-matrix.md',
    'research-modeling-open-issues.md', 'plugin-future-work.md',
    'documentation-inventory.md')) + (RECORDER_DOC, WEATHER_DOC, PYBADA_DOC)
RETIRED_TOPOLOGY = ('research/reproducibility', 'docs/research-consolidation')
OLD_SCHEMAS = ('samples-v7', 'samples-v8', 'samples-v9', 'samples-v10')


def test_active_documentation_uses_only_v11_and_current_topology():
    errors = []
    for path in ACTIVE_DOCS:
        text = path.read_text(encoding='utf-8')
        for token in OLD_SCHEMAS:
            if token in text:
                errors.append(f'{path}: obsolete schema {token}')
        for token in RETIRED_TOPOLOGY:
            if token in text:
                errors.append(f'{path}: retired topology {token}')
    assert not errors, '\n'.join(errors)


def test_documentation_links_to_local_markdown_exist():
    missing = []
    for path in ACTIVE_DOCS:
        for target in re.findall(r'\[[^]]+\]\(([^)#]+\.md)(?:#[^)]+)?\)',
                                 path.read_text(encoding='utf-8')):
            if not (path.parent / target).resolve().exists():
                missing.append(f'{path}: {target}')
    assert not missing, '\n'.join(missing)


def test_recorder_documentation_is_plugin_owned():
    assert RECORDER_DOC.is_file()


def test_nwp_documentation_is_plugin_owned():
    assert WEATHER_DOC.is_file()
    assert not Path('scripts/README-weather.md').exists()


def test_pybada_documentation_is_plugin_owned():
    assert PYBADA_DOC.is_file()


def test_pybada_documentation_links_exist():
    text = PYBADA_DOC.read_text(encoding='utf-8')
    for target in re.findall(r'\[[^]]+\]\(([^)#]+\.md)(?:#[^)]+)?\)', text):
        assert (PYBADA_DOC.parent / target).resolve().is_file(), target
