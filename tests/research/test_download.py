from contextlib import nullcontext

import pytest

from bluesky.plugins.meteo.download import DownloadError, atomic_download


class Response:
    def __init__(self, chunks, length=None, error=None):
        self.chunks = chunks
        self.headers = {} if length is None else {'content-length': str(length)}
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        if self.error:
            raise self.error

    def iter_content(self, chunk_size):
        yield from self.chunks


class Session:
    def __init__(self, response):
        self.response = response
        self.timeout = None

    def get(self, url, stream, timeout):
        self.timeout = timeout
        return self.response


def test_atomic_download_validates_then_renames(tmp_path):
    session = Session(Response([b'abc', b'def'], length=6))
    target = tmp_path / 'weather.grib'
    atomic_download(session, 'https://example.invalid/weather', target,
                    lambda path: path.read_bytes() == b'abcdef')
    assert target.read_bytes() == b'abcdef'
    assert not (tmp_path / 'weather.grib.part').exists()
    assert session.timeout == (10, 120)


def test_atomic_download_rejects_partial_content(tmp_path):
    target = tmp_path / 'weather.grib'
    with pytest.raises(DownloadError):
        atomic_download(Session(Response([b'abc'], length=8)), 'url', target, lambda path: None)
    assert not target.exists()
    assert not (tmp_path / 'weather.grib.part').exists()


def test_atomic_download_rejects_invalid_dataset(tmp_path):
    target = tmp_path / 'weather.grib'
    with pytest.raises(ValueError):
        atomic_download(Session(Response([b'bad'], length=3)), 'url', target,
                        lambda path: (_ for _ in ()).throw(ValueError('invalid dataset')))
    assert not target.exists()
