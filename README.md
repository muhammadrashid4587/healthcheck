# healthcheck

A tiny self-hosted service health monitor with a status dashboard.
Pure Python 3 standard library, zero dependencies.

It reads a list of HTTP/TCP endpoints from a config file, checks them on
a schedule (HTTP status + latency, TCP connect), tracks uptime per
endpoint, and serves a small status dashboard over `http.server`.

## What it is

- A monitor for a handful of your own services, running on one box.
- HTTP checks (ok on 2xx/3xx, with status code and latency recorded).
- TCP connect checks for services without HTTP.
- Per-endpoint uptime % and recent check history.
- Uptime % over the last 24 hours and the last 7 days, so a recent
  outage shows up even when the all-time figure still looks fine.
- A plain HTML dashboard: endpoint, status, uptime %, latency, last check.
  An endpoint whose dependency is not up shows SKIPPED, with the
  dependency named — a skip is not downtime.

## What it is not

- Not a replacement for a real observability platform (no metrics
  pipeline, no distributed tracing). Alerts are log lines plus an
  optional webhook — no email, no SMS, no PagerDuty.
- History is in-memory while the process runs, unless `history_file`
  is set (see Config) — then check history is loaded on startup and
  saved after every check round, so uptime survives restarts. A
  corrupt or missing history file logs a warning and starts empty.

## Quickstart

```sh
cp config.example.json config.json   # edit the endpoints
python3 -m healthcheck.cli --config config.json
```

Open http://127.0.0.1:8080/ for the dashboard. Check every endpoint
once and exit:

```sh
python3 -m healthcheck.cli --config config.json --once
```

## Config

```json
{
  "interval_seconds": 30,
  "history": 100,
  "endpoints": [
    {"name": "example", "type": "http", "url": "https://example.com", "timeout": 5},
    {"name": "db", "type": "tcp", "host": "127.0.0.1", "port": 5432, "timeout": 5}
  ]
}
```

- `interval_seconds` — seconds between check rounds (default 30).
- `history` — checks kept per endpoint (default 100).
- `history_file` — path to a JSON file for persisting check history
  across restarts (optional; default is in-memory only). The
  `--history-file` flag overrides it. The file is written atomically
  after every check round and on shutdown.
- `webhook_url` — URL to POST a JSON alert to when an endpoint changes
  state (optional; default is log lines only). The `--webhook-url`
  flag overrides it. The payload is
  `{"endpoint", "event" ("down" or "recovered"), "ok", "previous_ok",
  "latency_ms", "status_code", "error", "checked_at"}`. The first
  check of an endpoint sets its baseline and sends nothing; a failed
  delivery is logged as a warning and never stops the monitor.
- `http` endpoint — `url` required; ok on status 200–399.
  `cafile`, `cert`, and `key` are optional: `cafile` is a PEM bundle
  of certificate authorities to trust for an `https://` endpoint
  (for a private or self-signed CA); `cert` and `key` are a client
  certificate and private key for mutual TLS. Without them, an
  `https://` endpoint is verified against the system CA store, as
  before. A bad cert file makes the check fail, never crash. The
  TLS options are ignored for plain `http://` URLs.
- `tcp` endpoint — `host` and `port` required; ok when the connect
  succeeds.
- `depends_on` — the name of another endpoint in the same config
  (optional, either type). When the dependency is DOWN, SKIPPED, or
  has never been checked, the dependent is not checked at all: it is
  recorded as SKIPPED, shown as SKIPPED on the dashboard, excluded
  from uptime math, and fires no alert — a dependent cannot be "down"
  when its dependency is what failed. When the dependency recovers,
  the dependent is checked again on the next round. Skips propagate
  down a chain (A down → B skipped → C skipped). A `depends_on`
  naming an unknown endpoint, or any dependency cycle (including a
  self-dependency), is rejected at config load with a clear error —
  the monitor refuses to start rather than check a broken chain.

## Dashboard

`GET /` renders a table with one row per endpoint: name, current status
(UP / DOWN / SKIPPED / UNKNOWN), the endpoint it depends on (or `-`),
uptime % over the recorded history, uptime %
over the last 24 hours and the last 7 days (a `-` when no checks fall
in that window — no data is not downtime), most recent
latency, a sparkline of the last 20 recorded latencies, and last check
time. Skipped checks are recorded in history with the dependency that
caused the skip, but they never count in uptime and never appear in
the sparkline. Any other path returns 404.

## Tests

```sh
make test   # python3 -m unittest discover -s tests -v
```

## Roadmap

- [x] Configurable alert hooks (webhook URL) — done in 0.4.0.
- [x] HTTPS endpoints with custom CA / client certificates — done
  in 0.5.0.
- [x] Windowed uptime (24h / 7d) per endpoint — done in 0.6.0.
- [x] Dependent checks (skip when the dependency is not UP) — done
  in 0.7.0.
- [ ] Flap detection: only alert after N consecutive failures, so a
  single blip does not page anyone.

## Changelog

- **0.7.0** — Dependent checks: an endpoint can declare
  `depends_on` naming another endpoint, and when the dependency is
  DOWN, SKIPPED, or never checked, the dependent is recorded as
  SKIPPED instead of being checked — not DOWN, not counted against
  uptime, no alert. Skips propagate down a chain and the dependent
  resumes on the next round after the dependency recovers. Unknown
  dependency names and dependency cycles are rejected at config
  load. The dashboard shows a SKIPPED status and a Depends on
  column; history entries carry the skip and the dependency name.

- **0.6.0** — Windowed uptime on the dashboard: `History.uptime_pct_window(name, seconds)` reports the share of up checks in the last N seconds, and the dashboard shows Uptime 24h and Uptime 7d columns next to the all-time figure. A window with no recorded checks shows `-`, not 0%.

- **0.5.0** — HTTPS endpoints with a custom CA bundle and client
  certificates: `cafile`, `cert`, and `key` per `http` endpoint.
  `cafile` trusts a private/self-signed CA, `cert` + `key` do
  mutual TLS. Without them, `https://` checks verify against the
  system CA store as before; a bad cert file fails the check
  instead of crashing. Tested against a local TLS server with
  openssl-generated certs — no live external server needed.

- **0.4.0** — Configurable alert hooks: `webhook_url` in the config
  (or `--webhook-url`) POSTs a JSON payload to the given URL when an
  endpoint changes state (up -> down or down -> up). The first check
  of an endpoint sets its baseline and sends nothing; a failed
  delivery logs a warning and never stops the monitor.
- **0.3.0** — Per-endpoint latency sparkline on the dashboard: an
  inline SVG of the last 20 recorded latencies per endpoint, scaled
  to that endpoint's own min/max (flat when history is flat, `-`
  with fewer than two points).
- **0.2.0** — Persist history to disk across restarts: `history_file`
  in the config (or `--history-file`), loaded on startup, saved after
  every check round and on shutdown. Corrupt history files log a
  warning and start empty instead of crashing.
