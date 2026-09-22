"""Standalone NWP ownership and import-isolation contract."""

import subprocess
import sys


def test_nwp_plugins_load_without_recorder_or_pybada():
    script = (
        "import sys; "
        "import bluesky.plugins.meteo as meteo; "
        "import bluesky.plugins.windecmwf; "
        "import bluesky.plugins.windgfs; "
        "assert not hasattr(meteo, 'SCHEMA_VERSION'); "
        "assert not [name for name in sys.modules if "
        "name.startswith(('bluesky.plugins.recorder', 'bluesky.plugins.pybada'))]"
    )
    subprocess.run([sys.executable, '-c', script], check=True)
