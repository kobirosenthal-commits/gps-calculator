"""Shared pytest fixtures.

Sets test/no-network environment flags *before* importing `app`, per the
side-effect-free-import contract: importing `app` must never start network
threads on its own (that only happens via `start_background_workers()`,
called explicitly by `main.py`). Setting GPS_CALCULATOR_NO_WORKERS=1 here
additionally documents/guarantees that even an explicit call to
`start_background_workers()` made from a test is a no-op.
"""
import os
import threading

os.environ.setdefault('GPS_CALCULATOR_NO_WORKERS', '1')

import pytest

import app as app_module


@pytest.fixture
def client():
    app_module.app.config['TESTING'] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def _isolate_caches():
    """Snapshot and restore every shared in-memory cache dict around each
    test so that one test's writes (e.g. push_tles, load_almanac) can't leak
    into another test's assertions."""
    keys = [
        'almanac_data', 'rinex_data', 'rinex4_data', 'bei_d_data',
        'bei_cnv1_data', 'bei_cnv2_data', 'bei_cnv3_data', 'gal_inav_data',
        'gal_fnav_data', 'gps_cnv2_data', 'glo_fdma_data', 'glo_data',
        'bei_data', 'gal_data',
    ]
    snapshot = {k: getattr(app_module, k) for k in keys}
    yield
    for k, v in snapshot.items():
        setattr(app_module, k, v)


@pytest.fixture
def admin_token(monkeypatch):
    """Configure a known ADMIN_TOKEN for the duration of a test."""
    token = 'test-admin-secret'
    monkeypatch.setattr(app_module, 'ADMIN_TOKEN', token)
    return token
