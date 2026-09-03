#!/usr/bin/env python3
"""Validate a consecutive, bounded ERA5 cache without network access."""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import bluesky as bs

from bluesky.plugins.windecmwf import WindECMWF
from bluesky.tools.aero import R


def _slot(value):
    try:
        return datetime.strptime(value, '%Y%m%dT%H').replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('expected YYYYMMDDTHH') from exc


def validate(cache, start, end, bounds):
    if end < start:
        return ['end slot is earlier than start slot'], []
    provider = object.__new__(WindECMWF)
    provider.cache = Path(cache).expanduser().resolve()
    errors = []
    summaries = []
    reference_horizontal_axes = None
    reference_level_count = None
    vertical_domains = []
    current = start
    while current <= end:
        paths = [provider._path(current, bounds, index)
                 for index, _ in enumerate(provider._areas(bounds))]
        missing = [str(path) for path in paths if not path.is_file()]
        if missing:
            errors.extend(f'missing {path}' for path in missing)
            current += timedelta(hours=1)
            continue
        try:
            cubes = [provider._read_validated(path, current, area)
                     for path, area in zip(paths, provider._areas(bounds))]
            cube = cubes[0] if len(cubes) == 1 else provider._merge(cubes, current)
            center_lat = sum(bounds[::2]) / 2.0
            center_lon = sum(bounds[1::2]) / 2.0
            center_alt = float((cube.altitude[0] + cube.altitude[-1]) / 2.0)
            north, east, sample = cube.interpolate(
                [center_lat], [center_lon], [center_alt])
            if not bool(sample.valid[0]):
                errors.append(f'{current.isoformat()} center sample is invalid')
            values = (north[0], east[0], sample.temperature[0],
                      sample.pressure[0], sample.density[0])
            if not np.isfinite(values).all():
                errors.append(f'{current.isoformat()} center sample is non-finite')
            expected_rho = sample.pressure[0] / (R * sample.temperature[0])
            if not np.isclose(sample.density[0], expected_rho, rtol=1e-10):
                errors.append(f'{current.isoformat()} density is inconsistent with p/(R*T)')
            horizontal_axes = (cube.latitude, cube.longitude)
            if reference_horizontal_axes is None:
                reference_horizontal_axes = tuple(axis.copy() for axis in horizontal_axes)
                reference_level_count = len(cube.altitude)
            elif any(not np.array_equal(left, right)
                     for left, right in zip(reference_horizontal_axes, horizontal_axes)):
                errors.append(f'{current.isoformat()} horizontal grid differs from the first slot')
            if len(cube.altitude) != reference_level_count:
                errors.append(f'{current.isoformat()} vertical level count differs from the first slot')
            vertical_domains.append((float(cube.altitude[0]), float(cube.altitude[-1])))
            summaries.append(
                f'{current.isoformat()}: files={len(paths)} grid='
                f'{len(cube.altitude)}x{len(cube.latitude)}x{len(cube.longitude)} '
                f'center_alt={center_alt:.1f}m T={sample.temperature[0]:.3f}K '
                f'p={sample.pressure[0]:.3f}Pa windN/E='
                f'{north[0]:.3f}/{east[0]:.3f}m/s')
        except (OSError, ValueError, KeyError, IndexError) as exc:
            errors.append(f'{current.isoformat()} cannot be validated: {exc}')
        current += timedelta(hours=1)
    if vertical_domains:
        common_lower = max(lower for lower, _ in vertical_domains)
        common_upper = min(upper for _, upper in vertical_domains)
        if not common_lower < common_upper:
            errors.append('hourly grids have no common vertical domain')
        else:
            summaries.append(
                f'common vertical domain={common_lower:.1f}..{common_upper:.1f} m '
                f'({common_lower / 0.3048:.1f}..{common_upper / 0.3048:.1f} ft)')
    return errors, summaries


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('start', type=_slot, help='first slot, YYYYMMDDTHH')
    parser.add_argument('end', type=_slot, help='last slot, YYYYMMDDTHH')
    parser.add_argument('lat0', type=float)
    parser.add_argument('lon0', type=float)
    parser.add_argument('lat1', type=float)
    parser.add_argument('lon1', type=float)
    parser.add_argument('--cache', default='cache/weather/era5')
    parser.add_argument('--region', default='region')
    parser.add_argument('--pressure-levels', type=int, nargs='+')
    args = parser.parse_args(argv)
    bs.settings.era5_region = args.region
    if args.pressure_levels:
        bs.settings.era5_pressure_levels = args.pressure_levels
    bounds = (args.lat0, args.lon0, args.lat1, args.lon1)
    errors, summaries = validate(args.cache, args.start, args.end, bounds)
    print('\n'.join(summaries))
    if errors:
        print('INVALID ERA5 cache:\n  - ' + '\n  - '.join(errors))
        return 1
    slot_count = int((args.end - args.start).total_seconds() // 3600) + 1
    print(f'VALID ERA5 cache: {slot_count} consecutive hourly slots')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
