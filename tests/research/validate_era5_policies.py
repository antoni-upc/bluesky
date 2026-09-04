#!/usr/bin/env python3
"""Exercise ERA5 strict and interactive failure policies with cached data."""

from datetime import datetime, timezone
from pathlib import Path

import bluesky as bs
from bluesky.plugins.windecmwf import WindECMWF


def main():
    slot = datetime(2025, 8, 15, 12, tzinfo=timezone.utc)
    bounds = (40.0, -5.0, 45.0, 5.0)
    provider = WindECMWF()
    provider.cache = Path('cache/weather/era5').resolve()
    provider.load(*bounds, slot=slot)

    provider.strict = False
    sample = provider.get_atmosphere([41.0], [10.0], [3048.0], slot)
    if bool(sample.valid[0]) or sample.fallback_reason != 'OUTSIDE_REQUESTED_DOMAIN':
        raise SystemExit('INVALID: interactive outside-bounds sample lacks explicit fallback')

    provider.strict = True
    try:
        provider.get_atmosphere([41.0], [10.0], [3048.0], slot)
    except RuntimeError:
        pass
    else:
        raise SystemExit('INVALID: strict outside-bounds sample did not raise')

    bs.settings.meteo_time_autoupdate = False
    provider.load(*bounds, slot=slot)
    try:
        provider.get_atmosphere([41.0], [2.0], [3048.0],
                                datetime(2025, 8, 15, 13, tzinfo=timezone.utc))
    except RuntimeError:
        pass
    else:
        raise SystemExit('INVALID: strict expired time slot did not raise')
    if provider.cube is not None or not provider.unavailable_reason.startswith('TIME_SLOT_EXPIRED:'):
        raise SystemExit('INVALID: expired strict slot retained stale ERA5 data')
    print('VALID: ERA5 interactive spatial fallback, strict spatial rejection, '
          'and strict time expiry all pass without stale data')


if __name__ == '__main__':
    main()
