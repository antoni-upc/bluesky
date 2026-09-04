#!/usr/bin/env python3
"""Exercise GFS spatial, time-update, and interpolation failure policies."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import bluesky as bs
from bluesky.plugins.windgfs import WindGFS


CACHE = Path('cache/weather/gfs').resolve()
BOUNDS = (40.0, -5.0, 45.0, 5.0)
SLOT12 = datetime(2025, 8, 15, 12, tzinfo=timezone.utc)
SLOT18 = datetime(2025, 8, 15, 18, tzinfo=timezone.utc)


def provider(strict=False):
    result = WindGFS()
    result.cache = CACHE
    result.strict = strict

    def cached_fetch(slot):
        _, path = result._location(slot)
        if slot > SLOT18:
            raise FileNotFoundError(f'intentionally unavailable test slot: {path}')
        if not path.is_file():
            raise FileNotFoundError(f'not in cache: {path}')
        result._validate(path)
        return path

    result._fetch = cached_fetch
    return result


def assert_unavailable(result, reason):
    if result.cube is not None or not result.unavailable_reason.startswith(reason):
        raise SystemExit(f'INVALID: failure retained stale GFS data or wrong reason: '
                         f'{result.unavailable_reason!r}')


def main():
    original_update = bs.settings.meteo_time_autoupdate
    original_interpolation = bs.settings.meteo_time_interpolation
    original_source = bs.settings.windgfs_source
    try:
        bs.settings.windgfs_source = 'AWS'
        bs.settings.meteo_time_interpolation = False
        bs.settings.meteo_time_autoupdate = True

        interactive = provider(strict=False)
        interactive.load(*BOUNDS, slot=SLOT12)
        sample = interactive.get_atmosphere([41.0], [10.0], [3048.0], SLOT12)
        if bool(sample.valid[0]) or sample.fallback_reason != 'OUTSIDE_REQUESTED_DOMAIN':
            raise SystemExit('INVALID: interactive outside-bounds sample lacks explicit fallback')

        strict = provider(strict=True)
        strict.load(*BOUNDS, slot=SLOT12)
        try:
            strict.get_atmosphere([41.0], [10.0], [3048.0], SLOT12)
        except RuntimeError:
            pass
        else:
            raise SystemExit('INVALID: strict outside-bounds sample did not raise')

        bs.settings.meteo_time_autoupdate = False
        strict.load(*BOUNDS, slot=SLOT12)
        try:
            strict.get_atmosphere([41.0], [2.0], [3048.0], SLOT18)
        except RuntimeError:
            pass
        else:
            raise SystemExit('INVALID: TIMEUPDATE OFF did not reject the expired strict slot')
        assert_unavailable(strict, 'TIME_SLOT_EXPIRED:')

        bs.settings.meteo_time_autoupdate = True
        bs.settings.meteo_time_interpolation = False
        interactive = provider(strict=False)
        interactive.load(*BOUNDS, slot=SLOT18)
        missing_slot = SLOT18 + timedelta(hours=6)
        sample = interactive.get_atmosphere([41.0], [2.0], [3048.0], missing_slot)
        if bool(sample.valid[0]) or not sample.fallback_reason.startswith('TIME_SLOT_UNAVAILABLE:'):
            raise SystemExit('INVALID: missing next slot lacks explicit interactive fallback')
        assert_unavailable(interactive, 'TIME_SLOT_UNAVAILABLE:')

        strict = provider(strict=True)
        strict.load(*BOUNDS, slot=SLOT18)
        try:
            strict.get_atmosphere([41.0], [2.0], [3048.0], missing_slot)
        except RuntimeError:
            pass
        else:
            raise SystemExit('INVALID: missing next slot did not stop strict GFS')
        assert_unavailable(strict, 'TIME_SLOT_UNAVAILABLE:')

        bs.settings.meteo_time_interpolation = True
        interactive = provider(strict=False)
        interactive.load(*BOUNDS, slot=SLOT12)
        success, reason = interactive.advance_time_slot(
            SLOT18, lambda: interactive.load(*BOUNDS, slot=SLOT18))
        if success or not reason.startswith('TIME_SLOT_UNAVAILABLE:'):
            raise SystemExit('INVALID: missing interpolation successor lacks explicit fallback')
        assert_unavailable(interactive, 'TIME_SLOT_UNAVAILABLE:')

        strict = provider(strict=True)
        strict.load(*BOUNDS, slot=SLOT12)
        try:
            strict.advance_time_slot(SLOT18, lambda: strict.load(*BOUNDS, slot=SLOT18))
        except RuntimeError:
            pass
        else:
            raise SystemExit('INVALID: missing interpolation successor did not stop strict GFS')
        assert_unavailable(strict, 'TIME_SLOT_UNAVAILABLE:')
    finally:
        bs.settings.meteo_time_autoupdate = original_update
        bs.settings.meteo_time_interpolation = original_interpolation
        bs.settings.windgfs_source = original_source

    print('VALID: GFS interactive spatial fallback, strict spatial rejection, '
          'TIMEUPDATE OFF expiry, missing-slot handling, and missing interpolation '
          'successor policies all pass without stale data')


if __name__ == '__main__':
    main()
