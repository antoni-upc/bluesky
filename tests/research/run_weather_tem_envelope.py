#!/usr/bin/env python3
"""Run and validate the licensed ERA5/GFS by BADA 3/4 TEM envelope matrix."""

import argparse
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable
MATRIX = (
    ('era5-tem-envelope-bada3', 'ERA5', '3', 'E3ER', 'E3EO'),
    ('era5-tem-envelope', 'ERA5', '4', 'ERAR', 'ERAO'),
    ('gfs-tem-envelope-bada3', 'GFS', '3', 'G3ER', 'G3EO'),
    ('gfs-tem-envelope-bada4', 'GFS', '4', 'G4ER', 'G4EO'),
)


def environment(label):
    env = os.environ.copy()
    env['PYTHONNOUSERSITE'] = '1'
    env['PYTHONPATH'] = str(ROOT)
    env['MPLCONFIGDIR'] = f'/tmp/bluesky-mpl-weather-envelope-{os.getpid()}-{label}'
    return env


def execute(command, label):
    result = subprocess.run(command, cwd=ROOT, env=environment(label), text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            check=False)
    if result.returncode:
        raise RuntimeError(f'{label} failed ({result.returncode})\n{result.stdout}')
    return result.stdout.strip()


def run_scenario(entry):
    scenario = entry[0]
    execute([PYTHON, '-u', str(ROOT / 'tests/research/run_scenario_detached.py'),
             f'research/{scenario}'], scenario)
    return scenario


def validate_entry(entry):
    scenario, source, family, report_acid, off_acid = entry
    return execute([
        PYTHON, str(ROOT / 'tests/research/validate_era5_tem_run.py'),
        str(ROOT / 'output' / f'{scenario}.csv'), '--source', source,
        '--family', family, '--scenario', scenario, '--report-acid', report_acid,
        '--off-acid', off_acid], f'validator for {scenario}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--validate-only', action='store_true',
                        help='validate existing evidence without rerunning scenarios')
    parser.add_argument('--skip-unit', action='store_true',
                        help='skip focused dependency-free tests')
    parser.add_argument('--jobs', type=int, default=1,
                        help='scenario processes to run concurrently (default: 1)')
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    try:
        if not args.skip_unit:
            print(execute([
                PYTHON, '-m', 'pytest', '-q',
                'tests/research/test_weather_tem_envelope_validator.py',
                'tests/research/test_atmosphere.py',
                'tests/research/test_meteo_cube.py',
                'tests/research/test_recorder.py'], 'focused tests'))
        if not args.validate_only:
            with ThreadPoolExecutor(max_workers=args.jobs) as pool:
                futures = [pool.submit(run_scenario, entry) for entry in MATRIX]
                for future in as_completed(futures):
                    print(f'RAN {future.result()}', flush=True)
        for entry in MATRIX:
            print(validate_entry(entry))
        execute(['git', 'diff', '--check'], 'git diff --check')
    except (OSError, RuntimeError) as exc:
        parser.exit(1, f'WEATHER/TEM ENVELOPE GATE FAILED: {exc}\n')
    print('WEATHER/TEM ENVELOPE GATE PASSED: 4 licensed scenarios, 4 validators')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
