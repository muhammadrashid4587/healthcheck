# healthcheck

A tiny self-hosted service health monitor with a status dashboard.
Pure Python 3 standard library, zero dependencies.

It reads a list of HTTP/TCP endpoints from a config file, checks them on
a schedule (HTTP status + latency, TCP connect), tracks uptime per
endpoint, and serves a small status dashboard over `http.server`.

## What it is

- A monitor for a handful of your own services, running on one box.
- HTTP checks (ok on 2xx/3xx, with status code and latency recorded).
  An `http` endpoint can also assert on the response body
  (`expect_body` substring, `expect_body_regex` pattern): a 200
  that serves an error page is not "up".
- TCP connect checks for services without HTTP.
- Per-endpoint uptime % and recent check history.
- Flap detection: an endpoint only flips to DOWN (and only alerts)
  after 3 consecutive failed checks by default — a single blip is
  logged, not paged. The threshold is configurable per config.
- Maintenance windows: an endpoint can declare recurring weekly
  windows (`maintenance_windows`) during which it is not checked at
  all — no alert fires, no flap state changes, and uptime is
  untouched. The dashboard shows MAINTENANCE and lists the windows.
- Uptime % over the last 24 hours and the last 7 days, so a recent
  outage shows up even when the all-time figure still looks fine.
- A static status page: `python3 -m healthcheck.cli --config config.json --status-page status.html`
  renders the same view as the dashboard — current status, uptime
  24h/7d, latency sparkline — plus a recent-incidents list per
  endpoint, as one self-contained HTML file with no external
  references, so it can be hosted anywhere with no server. It is a
  snapshot of the persisted history; regenerate it to update it.
  Combine with `--once` to check first, then render.
  An endpoint whose dependency is not up shows SKIPPED, with the
  dependency named — a skip is not downtime.
- A JSON API: `GET /api/status` on the dashboard server returns the
  same data as JSON (status, uptime all-time / 24h / 7d, latency,
  last check, dependency, maintenance windows per endpoint), so
  scripts and other tools can consume the live status without
  scraping the HTML.

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

Generate a static status page from the persisted history (needs
`history_file` in the config, or `--history-file`), hostable anywhere
with no server:

```sh
python3 -m healthcheck.cli --config config.json --status-page status.html
```

Check once, then render the page from the fresh results:

```sh
python3 -m healthcheck.cli --config config.json --once --status-page status.html
```

## Config

```json
{
  "interval_seconds": 30,
  "history": 100,
  "endpoints": [
    {"name": "example", "type": "http", "url": "https://example.com", "timeout": 5,
     "maintenance_windows": [{"days": "sat", "start": "02:00", "end": "04:00"}]},
    {"name": "db", "type": "tcp", "host": "127.0.0.1", "port": 5432, "timeout": 5}
  ]
}
```

- `interval_seconds` — seconds between check rounds (default 30).
- `flap_threshold` — consecutive failed checks that confirm an
  outage (default 3). A failure below the threshold is recorded but
  unconfirmed: no alert, no uptime penalty, and the endpoint stays
  UP on the dashboard. A success at any point resets the count; a
  success after a confirmed outage is a recovery. Must be a
  positive integer. The `--flap-threshold` flag overrides it.
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
  delivery is logged as a warning and never stops the monitor. With
  flap detection, the alert fires only when the outage is confirmed
  (see `flap_threshold` above), not on a single failed check.
- `http` endpoint — `url` required; ok on status 200–399.
  `expect_body` is a substring the response body must contain and
  `expect_body_regex` a regex it must match; when both are set,
  both must hold. A 200 whose body fails the assertion is DOWN,
  with the failed assertion named in the error. The body is only
  read when an assertion is configured, and at most 1 MiB of it.
  A bad regex, a non-string value, or either key on a `tcp`
  endpoint is rejected at config load. A 4xx/5xx status is already
  DOWN, so no assertion runs there.
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
  When the dependency is inside a maintenance window, the dependent
  is skipped the same way, with the reason naming MAINTENANCE.
- `maintenance_windows` — a list of recurring weekly windows during
  which the endpoint is not checked (optional, either type). Each
  window is `{"start": "HH:MM", "end": "HH:MM"}` in the monitor's
  local time, with an optional `days` selector: a day abbreviation
  (`"sat"`), a list of them (`["mon", "wed"]`), `"weekdays"`, or
  `"weekend"`; no `days` means every day. A window whose end is
  earlier than its start runs overnight (e.g. `22:00`–`02:00` runs
  from 22:00 on its day to 02:00 the next morning). During a window
  the check is paused entirely: the round is recorded as
  maintenance, no alert fires, flap state is untouched, and uptime
  is unaffected — a paused check is neither up nor down. Bad windows
  (bad time format, unknown day, zero-length range, unknown key)
  are rejected at config load.

## Dashboard

`GET /` renders a table with one row per endpoint: name, current status
(UP / DOWN / SKIPPED / MAINTENANCE / UNKNOWN), the endpoint it depends
on (or `-`), its maintenance windows (or `-`), uptime % over the
recorded history, uptime %
over the last 24 hours and the last 7 days (a `-` when no checks fall
in that window — no data is not downtime), most recent
latency, a sparkline of the last 20 recorded latencies, and last check
time. Skipped checks are recorded in history with the dependency that
caused the skip, but they never count in uptime and never appear in
the sparkline; maintenance rounds are excluded the same way. A failure below the flap threshold likewise never
counts in uptime, and the endpoint still shows UP — it only flips to
DOWN once the threshold of consecutive failures is reached. Any other
path returns 404.

`GET /api/status` returns the same data as JSON, for scripts and
other tools: `{"generated_at": <epoch>, "endpoints": [...]}` with one
object per endpoint — `name`, `status` (UP / DOWN / SKIPPED /
MAINTENANCE / UNKNOWN, same rules as the dashboard), `depends_on`
(name or `null`), `maintenance_windows` (list of `start` / `end` /
`days`), `uptime_pct`, `uptime_24h_pct`, `uptime_7d_pct` (`null` when
no checks fall in that window), `latency_ms` (most recent, or `null`),
and `last_checked_at` (epoch seconds, or `null` when never checked).

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
- [x] Flap detection: only alert after N consecutive failures, so a
  single blip does not page anyone — done in 0.8.0.
- [x] Response-body assertions for HTTP endpoints
  (`expect_body`, `expect_body_regex`) — done in 0.9.0.
- [x] Maintenance windows: scheduled windows per endpoint during
  which checks are paused, alerts suppressed, and uptime untouched,
  shown on the dashboard — done in 0.10.0.
- [x] Static status-page generator: render the persisted history as
  a self-contained HTML status page (status, uptime 24h/7d,
  sparkline, recent incidents) via `--status-page`, hostable
  anywhere with no server — done in 0.11.0.
- [x] JSON API for the dashboard: `GET /api/status` returns the
  live status, uptime, latency, and last check per endpoint as
  JSON, so scripts and other tools can consume it without scraping
  the HTML — done in 0.12.0.

## Changelog

- **0.12.0** — JSON API for the dashboard: `GET /api/status`
  returns `{"generated_at", "endpoints": [...]}` with one object per
  endpoint — status (same rules as the dashboard, including
  unconfirmed failures still showing UP), uptime all-time / 24h /
  7d (`null` when no data in that window), most recent latency,
  last check time, dependency, and maintenance windows. Served by
  the same dashboard server, `Content-Type: application/json`;
  anything else still 404s.

- **0.11.0** — Static status-page generator: `--status-page PATH`
  writes a self-contained HTML status page from the persisted check
  history — current status per endpoint, uptime all-time / 24h / 7d,
  most recent latency, an inline latency sparkline, and a
  recent-incidents list (consecutive confirmed-down checks grouped
  into one incident, with start, end or "ongoing", duration, and
  failed-check count; skips, unconfirmed failures, and maintenance
  rounds are not incidents). The page has no external references
  (no scripts, stylesheets, images, or links), so it can be hosted
  anywhere or opened straight off disk. It is a snapshot —
  regenerate it to update it. Combines with `--once` to check
  first, then render. The file is written atomically, like history
  persistence.

- **0.10.0** — Maintenance windows: an endpoint can declare
  `maintenance_windows` — recurring weekly windows (`start` /
  `end` "HH:MM" in local time, optional `days` selector: a day,
  a list of days, `"weekdays"`, or `"weekend"`; overnight windows
  like 22:00–02:00 supported). During a window the endpoint is not
  checked: the round is recorded as maintenance, no alert fires,
  flap state is untouched, and uptime math and the latency sparkline
  exclude it. The dashboard shows a MAINTENANCE status and a
  Maintenance column listing each endpoint's windows. A dependency
  in maintenance skips its dependents, like a down dependency.
  Bad window specs are rejected at config load.

- **0.9.0** — Response-body assertions: an `http` endpoint can
  require the body to contain a substring (`expect_body`) or match
  a regex (`expect_body_regex`); a 200 that serves an error page is
  now DOWN, with the failed assertion named in the error. The body
  is only read when an assertion is configured (at most 1 MiB),
  non-UTF-8 bodies decode leniently instead of crashing, and a bad
  regex, a non-string value, or either key on a `tcp` endpoint is
  rejected at config load.

- **0.8.0** — Flap detection: an endpoint only counts as DOWN (and
  only alerts) after `flap_threshold` consecutive failed checks
  (default 3, configurable in the config or with
  `--flap-threshold`). Failures below the threshold are recorded as
  unconfirmed — logged, but no alert, no uptime penalty, and the
  endpoint stays UP on the dashboard. A success resets the counter;
  a success after a confirmed outage sends the recovery alert.
  Unconfirmed failures also do not skip dependents: a dependency is
  only "down" for its dependents once its outage is confirmed.
  Non-integer or non-positive thresholds are rejected at config
  load.
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
