from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RECORDER_DOC = ROOT / 'docs/plugin/recorder/recorder-v12-contract.md'


def test_recorder_documentation_is_plugin_owned():
    assert RECORDER_DOC.is_file()
    assert not (ROOT / 'docs/recorder-v12-contract.md').exists()
