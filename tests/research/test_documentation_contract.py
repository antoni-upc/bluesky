from pathlib import Path
import re


DOCS = Path('docs')
ACTIVE_DOCS = tuple(DOCS / name for name in (
    'research-plugins.md', 'current-plugin-architecture.md',
    'recorder-v11-contract.md',
    'bada-envelope-implementation.md', 'reproducibility-matrix.md',
    'research-modeling-open-issues.md', 'plugin-future-work.md',
    'documentation-inventory.md'))
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
