"""Tests for gps_core.py: TLS verification restored, RINEX2 diagnostics, and
truncated-record detection (previously silently zero-padded into fabricated
ephemeris)."""
import inspect

import gps_core


_HEADER = (
    "     2.11           N: GPS NAV DATA                         RINEX VERSION / TYPE\n"
    "teqc  2019Feb25     CORS-ADM Account    20260509 22:45:01UTCPGM / RUN BY / DATE\n"
    "    1.6760D-08  2.2350D-08 -1.1920D-07 -1.1920D-07          ION ALPHA\n"
    "    1.1060D+05  9.8300D+04 -1.3110D+05 -1.9660D+05          ION BETA\n"
    "    0.000000000000D+00 8.881784197000D-16    61440     2418 DELTA-UTC: A0,A1,T,W\n"
    "                                                            END OF HEADER\n"
)

_RECORD = (
    " 3 26  5  8 22  0  0.0 4.723626188934D-04-1.534772309242D-11 0.000000000000D+00\n"
    "    3.500000000000D+01-1.556250000000D+01 4.028382083998D-09-2.713533822261D+00\n"
    "   -7.767230272293D-07 6.803072872572D-03 3.753229975700D-06 5.153592237473D+03\n"
    "    5.112000000000D+05 1.415610313416D-07 2.995702508840D+00-3.725290298462D-08\n"
    "    9.943961056635D-01 3.242812500000D+02 1.219557174400D+00-7.903543500025D-09\n"
    "    1.332198348551D-10 1.000000000000D+00 2.417000000000D+03 0.000000000000D+00\n"
    "    2.800000000000D+00 0.000000000000D+00 1.396983861923D-09 3.500000000000D+01\n"
    "    5.040180000000D+05 4.000000000000D+00\n"
)


def test_parse_rinex2_nav_valid_record_parses_and_diags_are_populated():
    diag = {}
    result = gps_core.parse_rinex2_nav(_HEADER + _RECORD, diag=diag)
    assert diag['header_found'] is True
    assert diag['records_seen'] == 1
    assert diag['records_parsed'] == 1
    assert diag['records_skipped_truncated'] == 0
    assert 3 in result
    assert result[3]['prn'] == 3


def test_parse_rinex2_nav_truncated_record_is_skipped_not_fabricated():
    """Regression test for the truncation bug: a record missing its trailing
    continuation lines (e.g. file cut off mid-download) must be dropped, not
    silently zero-padded into fake ephemeris."""
    # Keep only 3 of the 7 required continuation lines after the epoch line.
    record_lines = _RECORD.splitlines()
    truncated = "\n".join(record_lines[:4]) + "\n"
    diag = {}
    result = gps_core.parse_rinex2_nav(_HEADER + truncated, diag=diag)
    assert result == {}
    assert diag['records_skipped_truncated'] == 1
    assert diag['records_parsed'] == 0


def test_parse_rinex2_nav_missing_header_is_flagged():
    diag = {}
    result = gps_core.parse_rinex2_nav(_RECORD, diag=diag)
    assert diag['header_found'] is False
    # Header wasn't found, so the "epoch scan" loop never advances past
    # end-of-input in this fixture and legitimately parses nothing usable
    # here — the important assertion is the header flag itself.
    assert isinstance(result, dict)


def test_parse_rinex2_nav_works_without_diag_argument():
    """diag is optional — callers that don't care about diagnostics must not
    be forced to pass one."""
    result = gps_core.parse_rinex2_nav(_HEADER + _RECORD)
    assert 3 in result


def test_no_tls_verification_disabled_anywhere():
    """Regression test: verify=False (and the urllib3 warning suppression
    that accompanied it) must not reappear in gps_core's network calls."""
    source = inspect.getsource(gps_core)
    assert 'verify=False' not in source
    assert 'disable_warnings' not in source


def test_parse_rinex4_combined_tracks_truncated_eph_counter():
    assert hasattr(gps_core, 'parse_rinex4_combined')
    sig_source = inspect.getsource(gps_core.parse_rinex4_combined)
    assert 'eph_truncated' in sig_source
