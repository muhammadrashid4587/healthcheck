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
- Prometheus metrics export: `GET /metrics` on the dashboard server
  emits the same live status in Prometheus text format — a per-endpoint
  up/down gauge, uptime ratios (all-time, 24h, 7d), a latency summary
  with p50/p95/p99 plus sum and count over the last 24 hours, and
  check totals by result — so a Prometheus server can scrape it.
- A JSON API: `GET /api/status` on the dashboard server returns the
  same data as JSON (status, uptime all-time / 24h / 7d, latency,
  last check, dependency, maintenance windows per endpoint), so
  scripts and other tools can consume the live status without
  scraping the HTML.
- SLA reporting: an endpoint can declare an `sla_target` (e.g.
  99.9); `python3 -m healthcheck.cli --config config.json
  --sla-report 2026-11` prints that calendar month's uptime per
  endpoint against its target, with the breach list — endpoints
  that fell short, and by how many percentage points.
- Config hot-reload: while the monitor is running, edit
  `config.json` and send SIGHUP (`kill -HUP <pid>`); the new
  endpoints, check interval, webhook, and flap threshold take
  effect before the next check round, and the dashboard follows.
  A bad edit never takes the monitor down — it is logged and the
  running config is kept. Flap state resets on a reload, and a
  changed `history_file` still needs a restart.
- Alert digest: instead of (or in addition to) one webhook per
  state change, an `alert_digest` block batches confirmed changes
  into a periodic summary (default every 15 minutes) — the changes
  in the window, the endpoints currently down, and any endpoints
  that flapped — POSTed as JSON to a webhook URL and/or rendered
  as a plain-text MIME email message and delivered over SMTP
  (STARTTLS by default, implicit TLS on port 465) when an SMTP
  host is configured; with no SMTP host the rendered message is
  logged instead, so an operator can pipe it to sendmail. A
  failed SMTP delivery is logged and never stops the monitor.

## What it is not

- Not a replacement for a real observability platform (no metrics
  pipeline, no distributed tracing). Alerts are log lines plus an
  optional per-change webhook and/or a periodic digest — the digest
  can be POSTed to a webhook and/or emailed over SMTP. There is no
  SMS or PagerDuty.
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

Print the monthly SLA report from the persisted history (needs
`history_file` in the config, or `--history-file`):

```sh
python3 -m healthcheck.cli --config config.json --sla-report 2026-11
```

With no month given, `--sla-report` uses the current month.

While the monitor is running, edit the config file and send
SIGHUP to reload it without restarting:

```sh
kill -HUP <pid>   # new endpoints, interval, webhook, and flap
                  # threshold apply before the next check round
```

The reloaded config is validated the same way as at startup: a
missing file, bad JSON, or an invalid endpoint (unknown
dependency, bad `sla_target`, bad regex, ...) is logged as a
warning and the running config is kept — the monitor never stops
on a bad edit. Endpoints added by the reload are checked from the
next round, removed ones stop being checked, and the dashboard
and `GET /api/status` show the new endpoint list. Two things do
not change on a reload: flap state resets (an endpoint mid-outage
needs the threshold's failures again before it re-alerts), and
`history_file` is not re-read — changing where history is stored
still needs a restart. SIGHUP reload is unavailable on Windows
and when the monitor is not on the main thread; the monitor runs
normally without it.

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
- `alert_digest` — batch confirmed state changes into a periodic
  digest instead of (or in addition to) one webhook per change
  (optional). The block is
  `{"interval_seconds": 900, "webhook_url": "https://hooks.example/digest",
  "email_to": ["ops@example.com"], "email_from": "monitor@example.com",
  "smtp_host": "mail.example.com", "smtp_port": 587,
  "smtp_user": "monitor", "smtp_password": "s3cret",
  "smtp_starttls": true}`,
  where `interval_seconds` defaults to 900 (15 minutes) and at
  least one of `webhook_url` / `email_to` is required. The SMTP
  keys are optional: `smtp_host` is the mail server, `smtp_port`
  defaults to 587, `smtp_user` / `smtp_password` are only used
  when a login is needed, and `smtp_starttls` defaults to true —
  the connection is upgraded with STARTTLS (port 465 always uses
  implicit TLS instead, no STARTTLS upgrade). Put credentials in
  the environment, not the config file: `HEALTHCHECK_SMTP_HOST`,
  `HEALTHCHECK_SMTP_PORT`, `HEALTHCHECK_SMTP_USER`,
  `HEALTHCHECK_SMTP_PASSWORD`, and `HEALTHCHECK_SMTP_STARTTLS`
  override the config keys when set. With no SMTP host configured
  anywhere, the digest email is only rendered and written to the
  log, so an operator can pick it up there or pipe it to sendmail.
  A failed SMTP delivery is logged as a warning and never stops
  the monitor, and the digest window still advances — a failed
  digest is not re-sent. Every interval the monitor sends one JSON
  payload —
  `{"type": "digest", "window_start", "window_end", "changes",
  "currently_down", "flapping", "summary"}` — with the confirmed
  changes in that window only (a change outside the window belongs
  to another digest), the endpoints currently down, and the
  endpoints that changed state three or more times in the window
  (flapping). An empty window — no changes and nothing currently
  down — sends nothing, so a quiet monitor stays quiet; a window
  with no changes but something still down still goes out, so an
  ongoing outage is not silent. When `email_to` is set, the same
  digest is rendered as a plain-text MIME message (From/To/Subject
  plus the changes, currently-down, and flapping lists) and
  delivered over SMTP when an SMTP host is configured, or written
  to the log when it is not. A bad digest block (non-object,
  unknown key, non-positive interval, non-http(s) webhook URL,
  empty recipient list, no destination at all, bad SMTP port or
  STARTTLS flag) is rejected at config load, and on a SIGHUP
  reload the running config is kept. The digest window resets on
  a reload, like flap state.
- `sla_target` — an uptime target in percent for the endpoint
  (optional, e.g. `99.9`), used only by the SLA report below.
  Must be a number above 0 and at most 100; a bad value is
  rejected at config load.
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
latency, a latency p50 / p95 / p99 column (nearest-rank percentiles
over the last 24 hours, `-` when no checks ran in that window), a sparkline
of the last 20 recorded latencies, and last check
time. Skipped checks are recorded in history with the dependency that
caused the skip, but they never count in uptime and never appear in
the sparkline or the percentiles; maintenance rounds are excluded the same way. A failure below the flap threshold likewise never
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
`latency_percentiles` (`{"p50", "p95", "p99"}` in ms over the
last 24 hours, or `null` when no checks ran in that window), and
`last_checked_at` (epoch seconds, or `null` when never checked).

`GET /metrics` returns the same data in Prometheus text format, for
scraping: `healthcheck_up` (gauge, 1 when the endpoint is currently
UP, 0 when DOWN — omitted for a never-checked, SKIPPED, or
maintenance endpoint), `healthcheck_uptime_ratio` (gauge, share of
checks that were up, all-time) plus `healthcheck_uptime_ratio_24h`
and `healthcheck_uptime_ratio_7d`, `healthcheck_latency_ms` (summary
with `quantile="0.5" / "0.95" / "0.99"` in ms over the last 24 hours,
plus `_sum` and `_count`), and `healthcheck_checks_total` (counter,
split by `result="success"` / `"failure"`). The same exclusion rules
apply as everywhere else: skipped and maintenance checks never ran,
so they are omitted from the up gauge and the totals and carry no
latency; a failure below the flap threshold still shows UP and counts
as a failure in the totals, since the check did fail. An endpoint
with no counted data in a window has no series for that window —
no data is not downtime.

## SLA report

`--sla-report [YYYY-MM]` prints one line per endpoint for a
calendar month (local time): the month's uptime % and how many
checks it is based on, against the endpoint's `sla_target`, and a
breach summary at the end. An endpoint with no target reports
"no target"; a month with no counted checks reports "no data" —
no data is not a breach. The same exclusion rules as uptime
everywhere else apply: skipped checks, unconfirmed failures below
the flap threshold, and maintenance rounds count neither for nor
against the month. With no month given, the current month is used.

```sh
python3 -m healthcheck.cli --config config.json --sla-report 2026-11
```

```
SLA report 2026-11
  example: 99.8% (412 checks) — BREACH target 99.9% (short 0.1pp)
  localhost-ssh: 100.0% (412 checks) — no target
1 breach: example
```

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
- [x] SLA reporting: per-endpoint `sla_target` in the config, and
  `--sla-report [YYYY-MM]` prints a calendar month's uptime per
  endpoint against its target, with the breach list — done in
  0.13.0.
- [x] Config hot-reload: edit the config file and send SIGHUP;
  the new endpoints, interval, webhook, and flap threshold apply
  before the next check round, and the dashboard follows. A bad
  config is logged and the running one kept, so a bad edit never
  takes the monitor down — done in 0.14.0.
- [x] Alert digest: batch confirmed state changes into a periodic
  summary (default every 15 minutes) — the changes in the window,
  the endpoints currently down, and any flapping endpoints —
  POSTed as JSON to a webhook URL and/or rendered as a plain-text
  MIME email message (logged when no SMTP host is configured;
  SMTP delivery arrived in 0.16.0). An empty window sends
  nothing; a bad digest config is rejected at load — done in
  0.15.0.
- [x] SMTP delivery for the digest email: `smtp_host` /
  `smtp_port` / `smtp_user` / `smtp_password` / `smtp_starttls`
  in the `alert_digest` block (or the `HEALTHCHECK_SMTP_*` env
  vars, which win — credentials belong in the environment, not
  the config file), STARTTLS by default, implicit TLS on port
  465, login when a user is set, and a failed delivery is logged
  and never stops the monitor — done in 0.16.0.

- [x] Latency percentiles: p50/p95/p99 per endpoint over the
  last 24 hours of the persisted history, shown on the dashboard
  and in `GET /api/status` — done in 0.17.0.
- [x] Prometheus-style metrics export: `GET /metrics` emits
  Prometheus text format — a per-endpoint up/down gauge, uptime
  ratios (all-time, 24h, 7d), a latency summary with p50/p95/p99
  plus sum and count over the last 24 hours, and check totals by
  result — so a Prometheus server can scrape the monitor — done
  in 0.18.0.
- [ ] Next: per-endpoint alert routing (different webhook per
  endpoint) or a second scrape target for the static status page.

## Changelog

- **0.18.0** — Prometheus metrics export: `GET /metrics` on the
  dashboard server returns Prometheus text format —
  `healthcheck_up` (gauge, 1 UP / 0 DOWN, omitted for never-checked,
  SKIPPED, or maintenance endpoints), `healthcheck_uptime_ratio`
  (all-time) plus `_24h` and `_7d` variants, a
  `healthcheck_latency_ms` summary with p50/p95/p99 plus `_sum` and
  `_count` over the last 24 hours, and `healthcheck_checks_total`
  split by `result="success"` / `"failure"`. Skipped and maintenance
  checks never ran and are excluded everywhere; failed checks count
  in the latency summary and as failures in the totals; a failure
  below the flap threshold still shows UP, same as the dashboard.
  `History.latency_summary()` backs the summary, and
  `latency_percentiles()` now delegates to it. Served with
  `Content-Type: text/plain; version=0.0.4`.

- **0.17.0** — Per-endpoint latency percentiles: `History.latency_percentiles(name)`
  returns nearest-rank p50/p95/p99 in ms over the checks that
  actually ran in the last 24 hours (skipped and maintenance rounds
  carry no latency and are excluded; failed checks count, since a
  slow error response is still a slow response). The dashboard gains
  a Latency p50 / p95 / p99 column, and `GET /api/status` gains a
  `latency_percentiles` object per endpoint (`null` when no checks
  ran in the window). No data is not a percentile: a never-checked
  endpoint shows `-` / `null`.

- **0.16.0** — SMTP delivery for the alert digest email: an
  `alert_digest` block can now set `smtp_host` (plus optional
  `smtp_port`, default 587, `smtp_user`, `smtp_password`, and
  `smtp_starttls`, default true), or the same settings via the
  `HEALTHCHECK_SMTP_HOST` / `_PORT` / `_USER` / `_PASSWORD` /
  `_STARTTLS` environment variables, which override the config —
  credentials belong in the environment, not the config file.
  Port 465 uses implicit TLS; other ports upgrade with STARTTLS
  when enabled. The rendered digest is sent with `smtplib` and
  delivery success or failure is logged; a failed delivery is a
  warning, never a crash, and the digest window still advances.
  With no SMTP host configured, the rendered message is still
  written to the log as before. Bad SMTP values (bad port, bad
  STARTTLS flag) are rejected at config load.

- **0.15.0** — Alert digest: an `alert_digest` block in the config
  batches confirmed state changes into a periodic summary (default
  every 15 minutes) — the changes in that window only, the
  endpoints currently down, and the endpoints that changed state
  three or more times in the window (flapping) — POSTed as JSON to
  a webhook URL and/or rendered as a plain-text MIME email message
  (written to the log; SMTP delivery arrived in 0.16.0). An
  empty window sends nothing; a window with no changes but
  something still down still goes out. A bad digest block is
  rejected at config load. Works alongside the per-change webhook,
  or on its own. The digest window resets on a SIGHUP reload,
  like flap state.

- **0.14.0** — Config hot-reload: send SIGHUP (`kill -HUP
  <pid>`) and the monitor reloads its config file before the
  next check round — new endpoints, `interval_seconds`,
  `webhook_url`, and `flap_threshold` take effect, and the
  dashboard and `GET /api/status` show the new endpoint list.
  The reload is validated like a startup load: a missing file,
  bad JSON, or invalid config logs a warning and keeps the
  running config, so a bad edit never stops the monitor. Flap
  state resets on a reload, and `history_file` is not re-read
  (changing it still needs a restart).

- **0.13.0** — SLA reporting: an endpoint can declare
  `sla_target` (a percentage, e.g. 99.9) in the config, and
  `--sla-report [YYYY-MM]` prints that calendar month's uptime
  per endpoint against its target, with a breach list (worst
  shortfall first, in percentage points). Skipped checks,
  unconfirmed failures below the flap threshold, and maintenance
  rounds are excluded, like everywhere else; a month with no
  counted checks reports "no data" and is not a breach. Bad
  targets (not a number, ≤ 0, or > 100) are rejected at config
  load. With no month given, the current month is used.

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
