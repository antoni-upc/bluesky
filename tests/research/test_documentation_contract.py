from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WEATHER_DOC = ROOT / 'docs/plugin/nwp-meteo/README-weather.md'


def test_nwp_documentation_is_plugin_owned():
    assert WEATHER_DOC.is_file()
    assert not (ROOT / 'scripts/README-weather.md').exists()
