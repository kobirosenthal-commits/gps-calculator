"""Tests for refresh_data.py: atomic writes (temp file + os.replace) so a
crash or concurrent read mid-write can't leave/see a torn file, plus the
strengthened TLE structural validation."""
import json
import os

import pytest

import refresh_data


def test_atomic_write_creates_file_with_content(tmp_path):
    target = tmp_path / "out.txt"
    refresh_data._atomic_write(str(target), "hello world")
    assert target.read_text(encoding='utf-8') == "hello world"


def test_atomic_write_leaves_no_tmp_file_behind(tmp_path):
    target = tmp_path / "out.txt"
    refresh_data._atomic_write(str(target), "data")
    leftovers = [p for p in os.listdir(tmp_path) if p != "out.txt"]
    assert leftovers == []


def test_atomic_write_replaces_existing_file_atomically(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("old content", encoding='utf-8')
    refresh_data._atomic_write(str(target), "new content")
    assert target.read_text(encoding='utf-8') == "new content"


def test_atomic_write_json_round_trips(tmp_path):
    target = tmp_path / "out.json"
    payload = {'ephemeris': {'1': {'a': 1}}, 'date': '2024-01-01'}
    refresh_data._atomic_write_json(str(target), payload)
    assert json.loads(target.read_text(encoding='utf-8')) == payload


def test_atomic_write_failure_does_not_leave_tmp_file(tmp_path, monkeypatch):
    """If writing raises partway through, the temp file must be cleaned up
    rather than left behind for a future run to stumble over."""
    target = tmp_path / "out.txt"

    class Boom(Exception):
        pass

    real_fdopen = os.fdopen

    def bad_fdopen(fd, *a, **kw):
        f = real_fdopen(fd, *a, **kw)
        f.close()
        raise Boom("simulated failure")

    monkeypatch.setattr(os, 'fdopen', bad_fdopen)
    with pytest.raises(Boom):
        refresh_data._atomic_write(str(target), "data")
    assert not target.exists()
    assert os.listdir(tmp_path) == []


def test_looks_like_tle_requires_line1_and_line2_prefixes():
    valid = "SAT NAME\n1 25544U 98067A   24001.00000000\n2 25544  51.6000 100.0000\n"
    assert refresh_data._looks_like_tle(valid) is True

    error_page = "Error: TLE data temporarily unavailable, try again later please" * 2
    assert refresh_data._looks_like_tle(error_page) is False

    assert refresh_data._looks_like_tle("") is False
    assert refresh_data._looks_like_tle("short") is False
