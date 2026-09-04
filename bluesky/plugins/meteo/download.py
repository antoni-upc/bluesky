"""Atomic HTTP cache helper for meteorological datasets."""

import os
from pathlib import Path


class DownloadError(RuntimeError):
    pass


def atomic_download(session, url, target, validate, timeout=(10, 120)):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + '.part')
    try:
        with session.get(url, stream=True, timeout=timeout) as response:
            response.raise_for_status()
            expected = response.headers.get('content-length')
            written = 0
            with part.open('wb') as stream:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        stream.write(chunk)
                        written += len(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if expected is not None and written != int(expected):
                raise DownloadError(f'Expected {expected} bytes, received {written}')
        validate(part)
        part.replace(target)
        return target
    except Exception:
        part.unlink(missing_ok=True)
        raise
