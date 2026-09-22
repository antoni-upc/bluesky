from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
PYBADA_DOC = ROOT / 'docs/plugin/pybada-tem/bada-envelope-implementation.md'


def test_pybada_documentation_is_plugin_owned():
    assert PYBADA_DOC.is_file()
    assert not (ROOT / 'docs/bada-envelope-implementation.md').exists()


def test_pybada_documentation_links_exist():
    text = PYBADA_DOC.read_text(encoding='utf-8')
    for target in re.findall(r'\[[^]]+\]\(([^)#]+\.md)(?:#[^)]+)?\)', text):
        assert (PYBADA_DOC.parent / target).resolve().is_file(), target
