#!/usr/bin/env python3
"""Run the complete licensed PyBADA regression and evidence-validation matrix."""

import argparse
import csv
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable

RECENT = {
    'acceleration': 'horizontal_acceleration',
    'saturation': 'horizontal_saturation',
    'joint-energy': 'joint_energy',
    'descent-energy': 'descent_energy',
    'conflict-energy': 'conflict_energy',
    'turn-load': 'turn_load',
    'turn-energy': 'turn_energy',
}

LEGACY = [
    ('pybada-envelope-mass', 'validate_envelope_run.py', []),
    ('pybada-envelope-abort', 'validate_envelope_run.py', ['--abort']),
    ('pybada-envelope-flight', 'validate_flight_envelope_run.py', []),
    ('pybada-envelope-flight-abort', 'validate_flight_envelope_run.py', ['--abort']),
    ('pybada-envelope-direct', 'validate_flight_envelope_run.py', ['--direct']),
    ('pybada-envelope-vertical', 'validate_vertical_envelope_run.py', []),
    ('pybada-envelope-vertical-direct', 'validate_vertical_envelope_run.py', ['--direct']),
    ('pybada-envelope-vertical-abort', 'validate_vertical_envelope_run.py', ['--abort']),
    ('pybada-envelope-lateral', 'validate_lateral_envelope_run.py', []),
    ('pybada-envelope-lateral-abort', 'validate_lateral_envelope_run.py', ['--abort']),
    ('pybada-envelope-highlift', 'validate_highlift_lateral_run.py', []),
    ('pybada-envelope-highlift-abort', 'validate_highlift_lateral_run.py', ['--abort']),
    ('pybada-envelope-approach', 'validate_approach_lateral_run.py', []),
    ('pybada-envelope-approach-abort', 'validate_approach_lateral_run.py', ['--abort']),
    ('pybada-envelope-terminal-observe', 'validate_terminal_observation.py', []),
    ('pybada-envelope-terminal', 'validate_terminal_lateral_run.py', []),
    ('pybada-envelope-takeoff-abort', 'validate_terminal_lateral_run.py', ['--abort', 'TO']),
    ('pybada-envelope-landing-abort', 'validate_terminal_lateral_run.py', ['--abort', 'LD']),
    ('pybada-route-speed-gui', 'validate_route_comparison.py', []),
    ('pybada3-envelope-observe', 'validate_bada3_observation.py', []),
    ('pybada3-envelope-mass', 'validate_bada3_mass.py', []),
    ('pybada3-envelope-mass-abort', 'validate_bada3_mass.py', ['--abort']),
    ('pybada3-envelope-flight', 'validate_bada3_flight.py', []),
    ('pybada3-envelope-flight-abort', 'validate_bada3_flight.py', ['--abort']),
    ('pybada3-envelope-direct', 'validate_bada3_flight.py', ['--direct']),
    ('pybada3-envelope-vertical', 'validate_bada3_vertical.py', []),
    ('pybada3-envelope-vertical-direct', 'validate_bada3_vertical.py', ['--direct']),
    ('pybada3-envelope-vertical-abort', 'validate_bada3_vertical.py', ['--abort']),
    ('pybada3-envelope-lateral', 'validate_bada3_lateral.py', []),
    ('pybada3-envelope-lateral-abort', 'validate_bada3_lateral.py', ['--abort']),
    ('pybada3-envelope-terminal', 'validate_bada3_lateral.py', ['--terminal']),
    ('pybada3-envelope-terminal-abort', 'validate_bada3_lateral.py', ['--terminal-abort']),
    ('pybada3-route', 'validate_bada3_route.py', []),
]


def matrix():
    entries = []
    for stem, validator in RECENT.items():
        for family in ('3', '4'):
            scenario = f'pybada-{stem}-bada{family}'
            entries.append((scenario, f'validate_{validator}_run.py',
                            ['--family', family]))
    entries.extend(LEGACY)
    for family in ('3', '4'):
        for label in ('dt100', 'dt050', 'dt020'):
            entries.append((f'pybada-convergence-bada{family}-{label}', None, []))
    return entries


def environment(scenario='common'):
    env = os.environ.copy()
    env['PYTHONNOUSERSITE'] = '1'
    env['PYTHONPATH'] = str(ROOT)
    env['MPLCONFIGDIR'] = f'/tmp/bluesky-mpl-revalidation-{os.getpid()}-{scenario}'
    return env


def execute(command, label, env=None):
    result = subprocess.run(command, cwd=ROOT, env=env or environment(),
                            text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise RuntimeError(f'{label} failed ({result.returncode})\n{result.stdout}')
    return result.stdout.strip()


def run_scenario(scenario):
    output = execute(
        [PYTHON, '-u', str(ROOT / 'tests/research/run_scenario_detached.py'),
         f'research/{scenario}'], scenario, environment(scenario))
    return scenario, output


def evidence_path(scenario):
    return ROOT / 'output' / f'{scenario}.csv'


def validate_generic(scenario):
    path = evidence_path(scenario)
    metadata_path = path.with_suffix('.metadata.json')
    event_path = path.with_suffix('.events.jsonl')
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    if metadata.get('schema_version') != 'samples-v10':
        raise RuntimeError(f'{scenario}: schema is not samples-v10')
    if metadata.get('scenario') != scenario:
        raise RuntimeError(f'{scenario}: metadata scenario mismatch')
    if metadata.get('rows') != len(rows) or not rows:
        raise RuntimeError(f'{scenario}: metadata/CSV row count is invalid')
    try:
        base_dt = float(metadata['base_timestep_s'])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f'{scenario}: base_timestep_s is missing') from exc
    if not 0.0 < base_dt <= 1.0:
        raise RuntimeError(f'{scenario}: implausible base timestep {base_dt}')
    raw_events = event_path.read_text(encoding='utf-8')
    events = [json.loads(line) for line in raw_events.splitlines() if line.strip()]
    if metadata.get('event_total') != len(events):
        raise RuntimeError(f'{scenario}: event count mismatch')
    if events and not raw_events.endswith('\n'):
        raise RuntimeError(f'{scenario}: event ledger is not newline-flushed')
    return f'{scenario}: {len(rows)} rows, {len(events)} events, dt={base_dt:g} s'


def specialized_validations(entries):
    results = []
    for scenario, validator, extra in entries:
        validate_generic(scenario)
        if validator:
            output = execute(
                [PYTHON, str(ROOT / 'tests/research' / validator),
                 str(evidence_path(scenario)), *extra], f'validator for {scenario}')
            results.append(output)
    for family in ('3', '4'):
        prefix = ROOT / 'output' / f'pybada-convergence-bada{family}'
        output = execute([
            PYTHON, str(ROOT / 'tests/research/validate_timestep_convergence_run.py'),
            '--family', family,
            '--dt100', f'{prefix}-dt100.csv',
            '--dt050', f'{prefix}-dt050.csv',
            '--dt020', f'{prefix}-dt020.csv'], f'convergence validator BADA {family}')
        results.append(output)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--validate-only', action='store_true',
                        help='Validate existing output without rerunning scenarios')
    parser.add_argument('--skip-unit', action='store_true',
                        help='Skip the dependency-free research pytest suite')
    parser.add_argument('--jobs', type=int, default=1,
                        help='Fresh scenario processes to run concurrently (default: 1)')
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    entries = matrix()
    try:
        if not args.skip_unit:
            print(execute([
                PYTHON, '-m', 'pytest', '-q', 'tests/research'],
                'dependency-free research suite'))
        if not args.validate_only:
            with ThreadPoolExecutor(max_workers=args.jobs) as pool:
                futures = {pool.submit(run_scenario, item[0]): item[0] for item in entries}
                for future in as_completed(futures):
                    scenario, _ = future.result()
                    print(f'RAN {scenario}', flush=True)
        results = specialized_validations(entries)
        for result in results:
            print(result)
        execute(['git', 'diff', '--check'], 'git diff --check')
    except (OSError, ValueError, KeyError, json.JSONDecodeError, RuntimeError) as exc:
        parser.exit(1, f'REVALIDATION FAILED: {exc}\n')
    print(f'REVALIDATION PASSED: {len(entries)} licensed scenarios, '
          f'{sum(item[1] is not None for item in entries) + 2} scientific validators')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
