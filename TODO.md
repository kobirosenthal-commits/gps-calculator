# GPS Calculator TODO

Audit date: 2026-09-11

This roadmap is based on a static review of the Python application, templates,
deployment files, and a focused offline validation run. Python syntax compilation
currently passes for all application modules.

## Bugs Found

### P0 - Fix first

- [ ] Fix custom GPS almanac dates returning HTTP 500.
  - Location: `app.py`, `load_almanac()`.
  - Cause: `datetime.strptime()` creates a naive datetime, then the route compares
    it with `datetime.now(timezone.utc)`, which is timezone-aware.
  - Reproduced with `POST /api/load-almanac` and date `2026-01-01`:
    `TypeError: can't compare offset-naive and offset-aware datetimes`.
  - Done when valid past dates return a normal API response, future dates return
    400, and both naive/aware datetime regressions are covered by tests.

- [ ] Make application imports side-effect free.
  - Location: `app.py`, the three module-level `threading.Thread(...).start()` calls.
  - Importing `app` immediately starts outbound TLE and RINEX refreshes. This makes
    tests access the network, starts duplicate workers under Flask's debug reloader,
    and gives every Gunicorn process its own refresh loops if worker count changes.
  - Move worker startup behind an explicit lifecycle function or a separate refresh
    service, and disable it in tests.
  - Done when `python -c "import app"` performs no network or file refresh work.

- [ ] Protect data-changing and expensive API endpoints.
  - Locations: `/api/refresh-data` and `/api/push-tles` in `app.py`.
  - Both endpoints are unauthenticated. A remote caller can trigger large downloads,
    parsing and file writes, or replace in-memory constellation TLEs.
  - Require an admin token or remove these endpoints from the public deployment;
    add request-size limits and rate limits.
  - Done when anonymous requests cannot mutate data or start refresh jobs.

### P1 - Correctness and reliability

- [ ] Fix the in-memory reload in `/api/refresh-data`.
  - Location: `app.py`, `refresh_data_endpoint()`.
  - The function uses `os.path` without importing `os`. Broad inner exception
    handlers hide the resulting `NameError`, so refresh may report success while
    `rinex_data` and RINEX 4 globals remain stale until restart/background reload.
  - Import dependencies at module scope and return a partial-failure status when any
    reload stage fails.

- [ ] Reject unknown constellations consistently.
  - Locations: `/api/load-almanac`, `/api/satellites`, `/api/satellite-detail`, and
    `/api/calculate`.
  - Several routes silently treat any unknown value as GPS, while `/api/push-tles`
    rejects it. Use one shared parser and return HTTP 400 for unsupported values.

- [ ] Fail clearly when GLONASS slot mapping is unavailable.
  - Locations: `_load_tle_constellation()` and `push_tles()` in `app.py`.
  - `fetch_glonass_slot_map()` returns an empty dictionary on an upstream failure;
    callers then discard every valid GLONASS TLE. The push route can report success
    with a count of zero.
  - Keep the previous cache on mapping failure and return/log a distinct degraded
    data-source error.

- [ ] Make BeiDou and Galileo TLE identity mapping resilient.
  - Location: `push_tles()` in `app.py`.
  - Satellites are silently dropped when upstream names do not match the exact
    `"(Cnn)"` or `"GSATnnnn"` patterns. The general background loader also labels
    BeiDou and Galileo by file order, which can change between refreshes.
  - Centralize identity mapping, use stable PRN/SVID mappings for all load paths,
    log unmatched records, and test representative upstream names.

- [ ] Fix the RINEX-versus-TLE validator's satellite matching.
  - Location: `validate_app.py`, `test_orbit_math()`.
  - The validator reads the NORAD catalog number from TLE line 1 and treats it as a
    GPS PRN. NORAD IDs and PRNs are different identifiers, so the comparison usually
    finds no matches, then still prints a successful zero-match summary.
  - Map GPS TLE names to PRNs and fail or skip explicitly when no satellites match.

- [ ] Treat malformed or truncated RINEX 4 records as diagnostics.
  - Location: `gps_core.py`, `_read_record_from_iter()` and
    `parse_rinex4_combined()`.
  - A record ending early is silently dropped. Track malformed/truncated counts,
    retain the previous known-good cache, and expose the failure in `/api/rinex-status`.

- [ ] Stop disabling TLS certificate verification.
  - Location: `gps_core.py`, TLE and GLONASS IAC requests.
  - `verify=False` plus globally suppressed warnings permits undetected response
    tampering. Restore certificate verification and handle source-specific failures.

- [ ] Use atomic writes for refreshed data files.
  - Location: `refresh_data.py`.
  - RINEX and JSON files are written directly while the application may read them.
    Write to a temporary file, flush/fsync as appropriate, then use `os.replace()`.
  - Validate downloaded content before replacing the last known-good file.

- [ ] Define a consistent cache concurrency model.
  - Location: global cache dictionaries in `app.py`.
  - Current whole-object assignments are safe from partial dictionary construction in
    CPython, but multi-field reads can span refresh generations and in-place caches
    such as `_glo_almanac_cache` are mutated field by field.
  - Introduce immutable cache snapshots plus a lock, or move shared data to an
    external store before increasing Gunicorn beyond one worker.

### P2 - API behavior and observability

- [ ] Set `MAX_CONTENT_LENGTH` and validate JSON field types and ranges.
  - Bound TLE text size, PRN ranges, date ranges, latitude/longitude, altitude, and
    timestamp length. Return stable 400/413 error payloads.

- [ ] Do not expose raw exception messages or tracebacks to clients.
  - Locations: the API 500 handler and `/api/refresh-data`.
  - Log detailed errors server-side and return a request ID plus a generic message.

- [ ] Add bounded exponential backoff with jitter to refresh workers.
  - Replace fixed two-minute failure retries, track consecutive failures, and preserve
    the last known-good cache.

- [ ] Expand `/api/rinex-status` to include every loaded dataset.
  - Add GPS CNV2, BeiDou CNV2/CNV3, GLONASS FDMA, ionosphere, system-time data,
    source, age, last success, last error, and malformed-record count.

- [ ] Clarify the time model in code and API documentation.
  - Document where values are UTC, GPS system time, Galileo system time, BDT, or
    GLONASS time. Add explicit conversion helpers and leap-second test vectors before
    applying offsets to propagation code.

## Test Plan

- [ ] Add `pytest` and a `tests/` directory.
- [ ] Add Flask API tests with all network calls and background workers disabled.
- [ ] Add parser fixtures for valid, malformed, truncated, and mixed RINEX records.
- [ ] Add propagation reference vectors for GPS Kepler, GLONASS RK4, and SGP4.
- [ ] Add timezone tests for UTC-aware input, naive API input, offsets, week rollover,
  and future-date rejection.
- [ ] Add stable constellation identity tests for TLE name/order changes.
- [ ] Convert `validate_app.py` checks into assertions or make warnings fail in CI
  when a required dataset/comparison is absent.
- [ ] Run syntax checks, unit tests, API tests, and a no-network import smoke test in CI.

## Feature Roadmap

### Phase 1 - Trustworthy data status

- [ ] Add a read-only `/healthz` endpoint for process health and a `/readyz` endpoint
  that reports whether minimum usable constellation data is loaded.
- [ ] Add a data-source dashboard showing source, epoch, file age, satellite count,
  last refresh result, and fallback currently in use.
- [ ] Show a visible stale/degraded badge in the UI instead of silently mixing old
  and current datasets.
- [ ] Add structured logs and metrics for refresh duration, source failures, parse
  counts, propagation failures, and API latency.

### Phase 2 - Better GNSS analysis

- [ ] Add an observer sky plot with azimuth/elevation tracks and configurable mask.
- [ ] Calculate visible-satellite count, DOP (GDOP/PDOP/HDOP/VDOP), and geometry
  quality for a receiver location and selected time.
- [ ] Add time playback with pause, step, speed control, and a shareable timestamp URL.
- [ ] Let users compare broadcast ephemeris with TLE propagation and display position
  delta over time with clear source/accuracy caveats.
- [ ] Add constellation and signal filters (LNAV, CNAV, CNV2, I/NAV, F/NAV, D1/D2,
  CNV1/CNV2/CNV3, FDMA).

### Phase 3 - Product and developer experience

- [ ] Update `README.md` for all four constellations, current templates, data pipeline,
  deployment model, refresh workflow, and complete API examples.
- [ ] Publish an OpenAPI schema and version the API before changing response shapes.
- [ ] Split the large `app.py` into route blueprints, cache/data services, refresh
  orchestration, and serializers without changing behavior.
- [ ] Move large inline template scripts/styles into versioned static assets and add a
  Content Security Policy.
- [ ] Add accessible keyboard navigation, focus states, non-color health indicators,
  responsive checks, and frontend error/retry states.
- [ ] Pin frontend libraries to reviewed versions and document/update third-party
  Cesium/Three.js assets deliberately.

## Suggested Delivery Order

1. Fix the custom-date crash and refresh reload bug.
2. Remove import-time workers and secure mutating endpoints.
3. Add the isolated test harness and repair the validator.
4. Harden identity mapping, file writes, TLS, cache snapshots, and retries.
5. Ship health/data-status features so users can judge data quality.
6. Build sky-plot, DOP, playback, and ephemeris-comparison features on the tested core.
