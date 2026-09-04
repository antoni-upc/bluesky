#!/usr/bin/env python3
"""Prepare one validated GFS analysis in the WINDGFS cache."""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bluesky as bs
from bluesky.plugins.windgfs import WindGFS


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument('date', help='analysis date in YYYYMMDD form')
    command.add_argument('cycle', choices=('00', '06', '12', '18'))
    command.add_argument('--source', choices=('NCEI', 'AWS'), default='AWS')
    command.add_argument('--base-url', default='', help='optional source URL override')
    command.add_argument('--cache', type=Path,
                         default=Path('cache/weather/gfs'),
                         help='cache directory (default: cache/weather/gfs)')
    command.add_argument('--dry-run', action='store_true',
                         help='show deterministic URL and target without downloading')
    command.add_argument('--until', metavar='YYYYMMDDTHH',
                         help='also prepare every six-hour cycle through this UTC cycle')
    return command


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        slot = datetime.strptime(args.date + args.cycle, '%Y%m%d%H').replace(
            tzinfo=timezone.utc)
    except ValueError as exc:
        raise SystemExit(f'Invalid GFS date {args.date!r}; expected YYYYMMDD') from exc
    bs.settings.windgfs_source = args.source
    bs.settings.windgfs_url = args.base_url
    end = slot
    if args.until:
        try:
            end = datetime.strptime(args.until, '%Y%m%dT%H').replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise SystemExit('--until must use YYYYMMDDTHH') from exc
        if end.hour not in (0, 6, 12, 18):
            raise SystemExit('--until must identify a 00, 06, 12, or 18 UTC cycle')
    if end < slot:
        raise SystemExit('--until cannot be earlier than the first cycle')
    provider = object.__new__(WindGFS)
    provider.cache = args.cache.expanduser().resolve()
    provider.cache.mkdir(parents=True, exist_ok=True)
    current = slot
    while current <= end:
        url, target = provider._location(current)
        state = 'cached' if target.is_file() else 'missing'
        print(f'GFS {state}: {target}')
        print(f'GFS source: {url}')
        if not args.dry_run:
            provider._fetch(current)
            print(f'GFS ready: {current.isoformat()} source={args.source}')
        current += timedelta(hours=6)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
