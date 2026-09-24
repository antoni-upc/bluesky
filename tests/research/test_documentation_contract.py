from pathlib import Path
import re


DOCS = Path('docs/plugin')
RECORDER_DOC = DOCS / 'recorder/recorder-v12-contract.md'
WEATHER_DOC = DOCS / 'nwp-meteo/README-weather.md'
PYBADA_DOC = DOCS / 'pybada-tem/bada-envelope-implementation.md'
ACTIVE_DOCS = tuple(DOCS / name for name in (
    'plugin-stack/research-plugins.md',
    'plugin-stack/current-plugin-architecture.md',
    'plugin-stack/reproducibility-matrix.md',
    'plugin-stack/documentation-inventory.md')) + (
        RECORDER_DOC, PYBADA_DOC, WEATHER_DOC)
RETIRED_TOPOLOGY = ('research/reproducibility', 'docs/research-consolidation')
OLD_SCHEMAS = ('samples-v7', 'samples-v8', 'samples-v9', 'samples-v10', 'samples-v11')


def test_active_documentation_uses_only_v12_and_current_topology():
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


def test_research_documentation_is_plugin_owned():
    assert all(path.is_file() and path.is_relative_to(DOCS)
               for path in ACTIVE_DOCS)
    assert not tuple(Path('docs').glob('*.md'))
    assert not Path('scripts/README-weather.md').exists()
