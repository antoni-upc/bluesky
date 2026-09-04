#!/usr/bin/env python3
"""Report level-flight TAS/force acceleration mismatch in recorded evidence."""

import argparse
import csv
from collections import defaultdict
from pathlib import Path


def audit(path, minimum_acceleration=0.05, maximum_vertical_speed=0.05):
    by_acid = defaultdict(list)
    with Path(path).open(newline='', encoding='utf-8') as stream:
        for row in csv.DictReader(stream):
            by_acid[row['acid']].append(row)
    findings = []
    summaries = []
    for acid, rows in sorted(by_acid.items()):
        rows.sort(key=lambda row: float(row['sim_time_s']))
        samples = []
        for previous, current in zip(rows, rows[1:]):
            dt = float(current['sim_time_s']) - float(previous['sim_time_s'])
            if dt <= 0.0:
                continue
            observed = (float(current['tas_m_s']) - float(previous['tas_m_s'])) / dt
            if (abs(observed) < minimum_acceleration or
                    abs(float(previous['vertical_speed_m_s'])) >= maximum_vertical_speed):
                continue
            force = ((float(previous['thrust_n']) - float(previous['drag_n'])) /
                     float(previous['mass_kg']))
            samples.append((observed, force))
        if not samples:
            summaries.append(f'{acid}: no material level-flight TAS changes')
            continue
        maximum = max(abs(observed - force) for observed, force in samples)
        summaries.append(
            f'{acid}: samples={len(samples)} observed_ax='
            f'{min(value[0] for value in samples):.3f}..'
            f'{max(value[0] for value in samples):.3f} m/s2 force_ax='
            f'{min(value[1] for value in samples):.6f}..'
            f'{max(value[1] for value in samples):.6f} m/s2 '
            f'max_abs_mismatch={maximum:.3f} m/s2')
        if maximum > minimum_acceleration:
            findings.append(f'{acid} mismatch {maximum:.3f} m/s2')
    return findings, summaries


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv', nargs='+', help='samples-v9 CSV evidence')
    args = parser.parse_args(argv)
    findings = []
    for value in args.csv:
        print(value)
        current, summaries = audit(value)
        print('\n'.join(f'  {summary}' for summary in summaries))
        findings.extend(f'{value}: {finding}' for finding in current)
    if findings:
        print('AUDIT FINDING: horizontal motion and recorded force disagree:\n  - ' +
              '\n  - '.join(findings))
        return 1
    print('AUDIT PASS: no material level-flight acceleration/force mismatch')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
