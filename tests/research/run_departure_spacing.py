#!/usr/bin/env python3
"""Bounded ISA/TEM departure pilot using the existing clean operational route."""
import argparse
import csv
import datetime as dt
import json
from pathlib import Path

import numpy as np
import bluesky as bs
from bluesky.core import simtime
from bluesky.traffic.asas.detection import ConflictDetection
from bluesky.traffic.asas.resolution import ConflictResolution
from tests.research.run_profile_matrix import (
    ROOT, configure_worker, external_sample, file_checksum, git_revision,
    validate_recorder_samples,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--second-departure', type=float, default=300)
    parser.add_argument('--single', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 0 <= args.second_departure < 900:
        parser.error('second departure must be in [0, 900) seconds')
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / 'workdir').mkdir(exist_ok=True)
    bs.init(mode='sim', detached=True, workdir=out / 'workdir',
            configfile=ROOT / 'settings.cfg')
    simtime.setdt(0.5)
    bs.sim.simdt = 0.5
    bs.sim.utc = dt.datetime(2025, 5, 1, 12)
    profile = json.loads((ROOT / 'experiments/profiles.json').read_text())['profiles']['pybada-recorder']
    _, recorder = configure_worker(profile, out, ROOT / 'cache/weather')
    defaults = {'detection': ConflictDetection.selected().__name__,
                'resolution': ConflictResolution.selected().__name__}
    assert ConflictDetection.setmethod('OFF')[0]
    assert ConflictResolution.setmethod('OFF')[0]

    def check_conflicts():
        assert ConflictDetection.selected() is ConflictDetection
        assert ConflictResolution.selected() is ConflictResolution
        assert not bs.traf.cd.confpairs
        assert not np.any(bs.traf.cr.active)

    check_conflicts()
    source = ROOT / 'experiments/example_ops_full_clean.scn'
    template = [line.split('>', 1)[1] for line in source.read_text().splitlines()
                if 'EXAMPLE' in line and '>ATDIST' not in line]
    departures = {'DEP1': 0.0}
    if not args.single:
        departures['DEP2'] = args.second_departure
    commands = [(0.0, 'CDMETHOD OFF'), (0.0, 'RESO OFF')]
    for acid, when in departures.items():
        commands.extend((when, command.replace('EXAMPLE', acid).replace(
            '{role:narrowbody}', 'A320-232')) for command in template)
    commands.sort(key=lambda item: item[0])
    def timestamp(seconds):
        return f'{int(seconds // 3600):02}:{int(seconds % 3600 // 60):02}:{seconds % 60:05.2f}'
    (out / 'scenario.scn').write_text('\n'.join(
        f'{timestamp(t)}>{c}' for t, c in commands) + '\n00:15:00.00>HOLD\n')
    bs.stack.set_scendata([t for t, _ in commands], [c for _, c in commands])
    samples = []
    first_samples = {}
    for step in range(1800):
        bs.sim.step()
        assert bs.sim.state == bs.OP, 'Unexpected simulation termination'
        check_conflicts()
        expected = {acid for acid, when in departures.items() if when <= step * 0.5}
        assert set(bs.traf.id) == expected, (bs.sim.simt, bs.traf.id, expected)
        for row in external_sample(step):
            first_samples.setdefault(row['acid'], row['sim_time_s'])
            assert all(np.isfinite(row[k]) for k in ('lat', 'lon', 'alt', 'tas', 'mass'))
            assert row['atmos_valid'] and row['atmos_source'] == 'ISA'
            samples.append(row)
    paths = recorder.recorder.stop()
    matched = validate_recorder_samples(paths[0], samples)
    with (out / 'external.csv').open('w') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(samples[0]))
        writer.writeheader()
        writer.writerows(samples)
    summary = {'revision': git_revision(), 'source_sha256': file_checksum(source),
               'profile': profile, 'duration_s': bs.sim.simt, 'timestep_s': 0.5,
               'scheduled_departures_s': departures, 'first_post_step_samples_s': first_samples,
               'initial_conflict_methods': defaults, 'conflict_processing_off_all_steps': True,
               'recorder_rows_matched': matched, 'quality': recorder.recorder.quality_status,
               'external_sha256': file_checksum(out / 'external.csv'),
               'limitation': 'Airborne clean departure/climb; no runway ground roll.'}
    (out / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
