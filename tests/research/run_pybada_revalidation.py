#!/usr/bin/env python3
"""Run the complete licensed PyBADA regression and evidence-validation matrix."""

import argparse
import csv
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tests.research.schema_compat import SCHEMA_VERSION
from tests.research.run_convergence_study import (DEFAULT_DTS, GATES, gate_scenario,
                                                  run_variant, study, variants)


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

# Timestep-convergence gates, each run at DEFAULT_DTS through the study tool.
CONVERGENCE_GATES = ('pybada-convergence-bada3', 'pybada-convergence-bada4')

# Scenarios whose correct outcome is an unplanned strict-failure HOLD, with the
# failure reason the detached runner must report.
EXPECTED_FAILURES = {
    'pybada-envelope-lateral-strict70': 'Unbounded TEM output',
}


def matrix():
    entries = []
    for stem, validator in RECENT.items():
        for family in ('3', '4'):
            scenario = f'pybada-{stem}-bada{family}'
            entries.append((scenario, f'validate_{validator}_run.py',
                            ['--family', family]))
    entries.extend(LEGACY)
    return entries


def execute(command, label):
    result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise RuntimeError(f'{label} failed ({result.returncode})\n{result.stdout}')
    return result.stdout.strip()


def run_scenario(scenario):
    command = [PYTHON, '-u', '-m', 'tests.research.run_scenario_detached',
               f'research/{scenario}']
    if scenario not in EXPECTED_FAILURES:
        return scenario, execute(command, scenario)
    result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=False)
    failure_log(scenario).write_text(f'exit={result.returncode}\n{result.stdout}',
                                     encoding='utf-8')
    return scenario, result.stdout.strip()


def failure_log(scenario):
    return ROOT / 'output' / f'{scenario}.runner.log'


def validate_expected_failure(scenario, reason):
    status, _, output = failure_log(scenario).read_text(encoding='utf-8').partition('\n')
    if status != 'exit=1' or 'UNPLANNED HOLD:' not in output or reason not in output:
        raise RuntimeError(f'{scenario}: expected an unplanned strict-failure HOLD '
                           f'({reason}), got {status}\n{output[-2000:]}')
    return f'{scenario}: unplanned strict-failure HOLD detected ({reason})'


def evidence_path(scenario):
    return ROOT / 'output' / f'{scenario}.csv'


def validate_generic(scenario):
    path = evidence_path(scenario)
    metadata_path = path.with_suffix('.metadata.json')
    event_path = path.with_suffix('.events.jsonl')
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    if metadata.get('schema_version') != SCHEMA_VERSION:
        raise RuntimeError(f'{scenario}: schema is not {SCHEMA_VERSION}')
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
    if any(row.get('dynamics_mode') == 'TEM' for row in rows):
        from tests.research.validate_numerical_run import validate as validate_numerical
        audit = validate_numerical(rows, events)
        audit_path = path.with_suffix('.numerical-audit.json')
        audit_path.write_text(json.dumps(audit, indent=2, allow_nan=False) + '\n',
                              encoding='utf-8')
        if not audit['passed']:
            raise RuntimeError(
                f'{scenario}: numerical audit failed: {audit["errors"][:3]}')
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
    for scenario, reason in EXPECTED_FAILURES.items():
        results.append(validate_expected_failure(scenario, reason))
    for gate in CONVERGENCE_GATES:
        results.append(validate_convergence(gate))
    return results


def validate_convergence(gate):
    errors, report = study(gate_scenario(gate), run=False, **GATES[gate])
    if errors:
        raise RuntimeError(f'{gate}: timestep convergence failed: {errors[:3]}')
    return f'{gate}: first-order convergence for {len(report["aircraft"])} aircraft'


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
                scenarios = [item[0] for item in entries] + list(EXPECTED_FAILURES)
                futures = [pool.submit(run_scenario, scenario) for scenario in scenarios]
                futures += [pool.submit(run_variant, path)
                            for gate in CONVERGENCE_GATES
                            for path, _ in variants(gate_scenario(gate), DEFAULT_DTS).values()]
                for future in as_completed(futures):
                    result = future.result()
                    print(f'RAN {result[0] if isinstance(result, tuple) else result}',
                          flush=True)
        results = specialized_validations(entries)
        for result in results:
            print(result)
        execute(['git', 'diff', '--check'], 'git diff --check')
    except (OSError, ValueError, KeyError, json.JSONDecodeError, RuntimeError) as exc:
        parser.exit(1, f'REVALIDATION FAILED: {exc}\n')
    print(f'REVALIDATION PASSED: {len(entries)} licensed scenarios, '
          f'{sum(item[1] is not None for item in entries)} scientific validators, '
          f'{len(CONVERGENCE_GATES)} convergence gates, '
          f'{len(EXPECTED_FAILURES)} expected strict-failure gate(s)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
