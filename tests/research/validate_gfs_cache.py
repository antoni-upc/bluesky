#!/usr/bin/env python3
"""Validate consecutive cached GFS analyses without network access."""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from bluesky.plugins.windgfs import WindGFS
from bluesky.tools.aero import R


def _slot(value):
    try:
        slot = datetime.strptime(value, '%Y%m%dT%H').replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('expected YYYYMMDDTHH') from exc
    if slot.hour not in (0, 6, 12, 18):
        raise argparse.ArgumentTypeError('GFS slot must be 00, 06, 12, or 18 UTC')
    return slot


def validate(cache, start, end, point):
    if end < start:
        return ['end slot is earlier than start slot'], []
    provider = object.__new__(WindGFS)
    provider.cache = Path(cache).expanduser().resolve()
    errors = []
    summaries = []
    reference_axes = None
    current = start
    while current <= end:
        _, path = provider._location(current)
        if not path.is_file():
            errors.append(f'missing {path}')
            current += timedelta(hours=6)
            continue
        try:
            provider._validate(path)
            cube = provider._read(path, current)
            north, east, sample = cube.interpolate([point[0]], [point[1]], [point[2]])
            if not bool(sample.valid[0]):
                errors.append(f'{current.isoformat()} point sample is invalid')
            values = (north[0], east[0], sample.temperature[0],
                      sample.pressure[0], sample.density[0])
            if not np.isfinite(values).all():
                errors.append(f'{current.isoformat()} point sample is non-finite')
            expected_rho = sample.pressure[0] / (R * sample.temperature[0])
            if not np.isclose(sample.density[0], expected_rho, rtol=1e-10):
                errors.append(f'{current.isoformat()} density is inconsistent with p/(R*T)')
            axes = (cube.latitude, cube.longitude)
            if reference_axes is None:
                reference_axes = tuple(axis.copy() for axis in axes)
            elif any(not np.array_equal(left, right)
                     for left, right in zip(reference_axes, axes)):
                errors.append(f'{current.isoformat()} horizontal grid differs from first slot')
            summaries.append(
                f'{current.isoformat()}: grid={len(cube.altitude)}x{len(cube.latitude)}x'
                f'{len(cube.longitude)} point={point[0]:.4f},{point[1]:.4f},'
                f'{point[2]:.1f}m T={sample.temperature[0]:.3f}K '
                f'p={sample.pressure[0]:.3f}Pa windN/E='
                f'{north[0]:.3f}/{east[0]:.3f}m/s')
        except (ImportError, OSError, ValueError, KeyError, IndexError) as exc:
            errors.append(f'{current.isoformat()} cannot be validated: {exc}')
        current += timedelta(hours=6)
    return errors, summaries


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('start', type=_slot, help='first slot, YYYYMMDDTHH')
    parser.add_argument('end', type=_slot, help='last slot, YYYYMMDDTHH')
    parser.add_argument('latitude', type=float)
    parser.add_argument('longitude', type=float)
    parser.add_argument('altitude_m', type=float)
    parser.add_argument('--cache', default='cache/weather/gfs')
    args = parser.parse_args(argv)
    errors, summaries = validate(
        args.cache, args.start, args.end,
        (args.latitude, args.longitude, args.altitude_m))
    print('\n'.join(summaries))
    if errors:
        print('INVALID GFS cache:\n  - ' + '\n  - '.join(errors))
        return 1
    slot_count = int((args.end - args.start).total_seconds() // 21600) + 1
    print(f'VALID GFS cache: {slot_count} consecutive six-hour slots')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
