# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Every request now has its own transaction ID, and a reply is only accepted if its ID,
  unit, function, length and byte count match. All versions before this sent ID 0 and took
  whatever one read returned, so a late or unrelated reply could end up in the wrong
  registers. In my installation wrong values reached the long-term statistics of the PV
  yield sensors in Home Assistant.
- The SDongle sends an unsolicited frame with ID 0 on every new connection. It is now
  discarded, and the proxy waits for the session to start before its first request.
- After a timeout or an incomplete reply the connection is closed and opened again.
- Missing registers, or registers older than 120 seconds, return a Modbus gateway exception
  instead of zeros or old values.
- Replies to forwarded writes are checked for ID and echo.

### Added

- Plausibility checks for the cumulative and daily PV yield counters. A rejected counter
  only moves to a new value after three consistent reads.
- `MAX_PV_KW` and `SESSION_INIT_DELAY` environment variables.
- Tests in `tests/`, run with `python3 -m unittest discover tests`.

## [1.0.0] - 2026-08-17

First tagged release. The proxy has been running unchanged in production against a
SUN2000 with a LUNA2000 battery since 2026-07-21.

### Added

- Modbus TCP cache in front of a Huawei SUN2000 inverter: holds exactly one connection
  to the inverter and polls a configured register set on an interval (`POLL_INTERVAL`,
  default 10 s).
- Serves cached holding registers (FC3) to any number of concurrent Modbus TCP clients,
  with no additional load on the inverter. Clients can poll as fast as they like.
- Write-through for single-register writes (FC6), so control paths such as a
  surplus-heating controller keep working.
- Stale-cache behaviour: the last known values keep being served while the inverter is
  briefly unreachable, and the staleness is logged, instead of every client failing at
  once.
- Configuration entirely through environment variables: `SUN2000_HOST`, `SUN2000_PORT`,
  `SUN2000_UNIT_ID`, `LISTEN_HOST`, `LISTEN_PORT`, `POLL_INTERVAL`, `LOG_LEVEL`.
- Deployment units: `Dockerfile` (python:3.12-alpine, runs as `nobody`),
  `docker-compose.yml`, and a systemd service unit.
- No third-party Python dependencies — standard library only.

[1.0.0]: https://github.com/cloudapp-dev/sun2000-modbus-cache/releases/tag/v1.0.0
