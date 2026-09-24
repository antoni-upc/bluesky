#!/usr/bin/env python3
"""Run a timestep-convergence study of any scenario, or one of the fixed gates.

The scenario is rewritten once per timestep (its DT lines replaced and its
recorder output renamed), each variant runs in a fresh detached process and
passes the generic evidence and numerical audit, and the runs are compared
with tests/research/convergence.py. The report is written to
output/convergence/<scenario>/report.json.

Examples:
    python tests/research/run_convergence_study.py research/my-route \\
        --observable fuel_burn_kg --production-dt 0.05 --tolerance 5
    python tests/research/run_convergence_study.py pybada-convergence-bada4
"""

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tests.research.convergence import (DEFAULT_FIELDS, OBSERVABLES, ORDER_BAND, analyse,
                                        dt_label, summary)

ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable
DEFAULT_DTS = (0.10, 0.05, 0.025)
_EXACT_CLIMB = {'CV0': ('geometric_alt_m', 'pressure_alt_m')}
# Fixed regression gates: scenario under scenario/research and analysis settings.
GATES = {
    'pybada-convergence-bada3': dict(exact=_EXACT_CLIMB, observable='fuel_burn_kg',
                                     production_dt=0.05, tolerance=0.5),
    'pybada-convergence-bada4': dict(exact=_EXACT_CLIMB, observable='fuel_burn_kg',
                                     production_dt=0.05, tolerance=0.5),
    'era5-convergence-bada4': dict(exact=_EXACT_CLIMB, observable='fuel_burn_kg',
                                   production_dt=0.05, tolerance=0.5),
}


def scenario_path(scenario):
    path = Path(scenario).with_suffix('.scn')
    return path if path.is_absolute() else ROOT / 'scenario' / path


def variants(scenario, dts=DEFAULT_DTS):
    """Write one scenario per timestep and return ``{dt: (variant_path, csv_path)}``."""
    source = scenario_path(scenario)
    lines = source.read_text(encoding='utf-8').splitlines()
    starts = [line for line in lines if re.search(r'>\s*RECORDRESEARCH\s+START\b', line, re.I)]
    if len(starts) != 1:
        raise ValueError(f'{source.name}: expected exactly one RECORDRESEARCH START')
    for line in lines:
        match = re.search(r'>\s*RECORDRESEARCH\s+INTERVAL\s+([0-9.]+)', line, re.I)
        if match:
            interval = float(match.group(1))
            for dt in dts:
                if abs(interval / dt - round(interval / dt)) > 1e-9:
                    raise ValueError(f'recorder interval {interval} s is not a multiple of {dt} s')
    folder = ROOT / 'output' / 'convergence' / source.stem
    folder.mkdir(parents=True, exist_ok=True)
    result = {}
    for dt in dts:
        name = f'{source.stem}-{dt_label(dt)}'
        body = [line for line in lines if not re.search(r'>\s*DT\s', line, re.I)]
        body = [re.sub(r'(RECORDRESEARCH\s+START\s+)\S+', rf'\g<1>{name}.csv', line, flags=re.I)
                for line in body]
        path = folder / f'{name}.scn'
        path.write_text('\n'.join([f'# {dt} s variant of {source.name}',
                                   f'00:00:00.00>DT {dt}'] + body) + '\n', encoding='utf-8')
        result[dt] = (path, ROOT / 'output' / f'{name}.csv')
    return result


def run_variant(path, nonstrict=False):
    command = [PYTHON, '-u', '-m', 'tests.research.run_scenario_detached', str(path)]
    if nonstrict:
        command.append('--pybada-nonstrict')
    result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise RuntimeError(f'{path.stem} failed ({result.returncode})\n{result.stdout[-2000:]}')
    return path.stem


def study(scenario, *, dts=DEFAULT_DTS, run=True, jobs=3, nonstrict=False, **analysis):
    """Run (optionally) and analyse one study; return ``(errors, report)``."""
    from tests.research.run_pybada_revalidation import validate_generic
    runs = variants(scenario, dts)
    if run:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            list(pool.map(lambda item: run_variant(item[0], nonstrict), runs.values()))
    audits = [validate_generic(path.stem) for path, _ in runs.values()]
    errors, report = analyse({dt: csv for dt, (_, csv) in runs.items()}, **analysis)
    report['scenario'] = scenario_path(scenario).stem
    report['audits'] = audits
    out = ROOT / 'output' / 'convergence' / scenario_path(scenario).stem / 'report.json'
    out.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    return errors, report


def gate_scenario(name):
    return f'research/{name}'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('scenario', help='scenario relative to scenario/ (without .scn), an '
                        'absolute path, or a gate name: ' + ', '.join(GATES))
    parser.add_argument('--dt', type=float, nargs=3, default=DEFAULT_DTS,
                        help='three timesteps with a constant ratio (default 0.10 0.05 0.025)')
    parser.add_argument('--fields', nargs='+', default=list(DEFAULT_FIELDS))
    parser.add_argument('--exact', nargs='*', default=[], metavar='ACID:FIELD',
                        help='fields that must integrate exactly, e.g. CV0:geometric_alt_m')
    parser.add_argument('--observable', choices=OBSERVABLES)
    parser.add_argument('--production-dt', type=float)
    parser.add_argument('--tolerance', type=float)
    parser.add_argument('--order-band', type=float, nargs=2, default=ORDER_BAND)
    parser.add_argument('--validate-only', action='store_true',
                        help='analyse existing variant outputs without running them')
    parser.add_argument('--jobs', type=int, default=3)
    parser.add_argument('--pybada-nonstrict', action='store_true')
    args = parser.parse_args(argv)
    if args.scenario in GATES:
        scenario, analysis = gate_scenario(args.scenario), dict(GATES[args.scenario])
    else:
        exact = {}
        for item in args.exact:
            acid, field = item.split(':', 1)
            exact[acid] = exact.get(acid, ()) + (field,)
        scenario = args.scenario
        analysis = dict(exact=exact, observable=args.observable,
                        production_dt=args.production_dt, tolerance=args.tolerance)
    try:
        errors, report = study(scenario, dts=tuple(args.dt), run=not args.validate_only,
                               jobs=args.jobs, nonstrict=args.pybada_nonstrict,
                               fields=tuple(args.fields), order_band=tuple(args.order_band),
                               **analysis)
    except (OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError) as exc:
        parser.exit(1, f'CONVERGENCE STUDY FAILED: {exc}\n')
    print('\n'.join(summary(report)))
    if errors:
        parser.exit(1, 'CONVERGENCE STUDY FAILED:\n  - ' + '\n  - '.join(errors) + '\n')
    print(f'CONVERGENCE STUDY PASSED: {report["scenario"]}, {len(report["aircraft"])} aircraft')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
