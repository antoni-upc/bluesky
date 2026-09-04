#!/usr/bin/env python3
"""Prepare one validated ERA5 analysis in the WINDECMWF cache."""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bluesky.plugins.windecmwf import WindECMWF
import bluesky as bs


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument('date', help='analysis date in YYYYMMDD form')
    command.add_argument('hour', type=int, choices=range(24), metavar='HOUR')
    command.add_argument('lat0', type=float)
    command.add_argument('lon0', type=float)
    command.add_argument('lat1', type=float)
    command.add_argument('lon1', type=float)
    command.add_argument('--cache', type=Path,
                         default=Path('cache/weather/era5'),
                         help='cache directory (default: cache/weather/era5)')
    command.add_argument('--region', default='region',
                         help='safe human-readable cache label (for example ecac-core)')
    command.add_argument('--pressure-levels', type=int, nargs='+',
                         help='exact pressure levels in hPa (default: configured ERA5 levels)')
    command.add_argument('--dry-run', action='store_true',
                         help='show deterministic targets without downloading')
    command.add_argument('--until', metavar='YYYYMMDDTHH',
                         help='also prepare every hourly slot through this UTC hour')
    return command


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        slot = datetime.strptime(args.date, '%Y%m%d').replace(
            hour=args.hour, tzinfo=timezone.utc)
    except ValueError as exc:
        raise SystemExit(f'Invalid ERA5 date {args.date!r}; expected YYYYMMDD') from exc
    bounds = (args.lat0, args.lon0, args.lat1, args.lon1)
    end = slot
    if args.until:
        try:
            end = datetime.strptime(args.until, '%Y%m%dT%H').replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise SystemExit('--until must use YYYYMMDDTHH') from exc
    if end < slot:
        raise SystemExit('--until cannot be earlier than the first slot')
    provider = object.__new__(WindECMWF)
    provider.cache = args.cache.expanduser().resolve()
    bs.settings.era5_region = args.region
    if args.pressure_levels:
        bs.settings.era5_pressure_levels = args.pressure_levels
    if not args.dry_run:
        credential_file = Path(__import__('os').environ.get(
            'CDSAPI_RC', '~/.cdsapirc')).expanduser()
        if not credential_file.is_file() and not __import__('os').environ.get('CDSAPI_KEY'):
            raise SystemExit(f'CDS credentials not found at {credential_file}')
    provider.cache.mkdir(parents=True, exist_ok=True)
    current = slot
    while current <= end:
        targets = [provider._path(current, bounds, index)
                   for index, _ in enumerate(provider._areas(bounds))]
        for target in targets:
            state = 'cached' if target.is_file() else 'missing'
            print(f'ERA5 {state}: {target}')
        if not args.dry_run:
            provider._fetch(current, bounds)
            print(f'ERA5 ready: {current.isoformat()} bounds={bounds}')
        current += timedelta(hours=1)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
