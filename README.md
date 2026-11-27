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
  that serves an error page is not "up". It can also assert on the
  exact status code (`expect_status`, e.g. 204 for a health
  endpoint that never returns 200): any other status is DOWN,
  with the expected and actual codes named. The dashboard and
  `GET /api/status` show the expected vs actual status per
  endpoint. An endpoint can also choose its HTTP method
  (`method`, e.g. `HEAD` for a headers-only check or `POST` for
  a health endpoint that only answers to POST); GET is the
  default, and the check sends no body. An endpoint can also send
  extra request headers with its checks (`headers`, e.g.
  `{"Authorization": "Bearer ..."}` for a health endpoint behind
  auth, or `{"X-Api-Key": "..."}`); the values are sent verbatim
  and are never shown anywhere — the dashboard and `GET
  /api/status` list only the header names, since a value is
  often a secret.
- Per-endpoint latency budgets (`max_latency_ms`): an endpoint
  can declare the slowest it may answer (e.g. 500 ms); a check
  that answers slower is DOWN, with the actual latency and the
  budget named in the error, and goes through the same flap
  rules as any other failure — a single slow check is
  unconfirmed, and the endpoint only flips to DOWN (and only
  alerts) after the threshold's consecutive slow checks. The
  dashboard gains a Latency budget column and `GET /api/status`
  gains `max_latency_ms` per endpoint, so a slow endpoint shows
  both numbers side by side. Set in the config file or with
  `--max-latency NAME=MS` (repeatable, flag wins); a bad value
  is rejected at config load, and a SIGHUP reload picks up
  changes.
- Per-endpoint p95 latency alerts (`latency_p95_threshold_ms`):
  an endpoint can declare the p95 latency it must stay within
  (e.g. 400 ms); when the p95 over the last 24 hours crosses it,
  the endpoint is in a latency-alert state — advisory only, it
  stays UP — and one webhook alert fires per crossing, to the
  endpoint's own `webhook` URL when it has one, else the global
  `webhook_url`; a sustained breach pages exactly once, and a
  p95 that falls back below the threshold re-arms, so the next
  crossing pages again. Shown as `ALERT 812.3 > 400 ms` in the
  dashboard's Latency p95 alert column (with the time the alert
  fired appended) and as `latency_p95_alert: true` in `GET
  /api/status`. Set in the config file or with
  `--latency-p95-threshold NAME=MS` (repeatable, flag wins); a
  bad value is rejected at config load, and a SIGHUP reload
  picks up changes.
- Dry-run mode (`--check [NAME]`): validate the config, run one
  check pass over every endpoint (or just the named one, with its
  dependencies), print the per-endpoint verdict — UP/DOWN with
  status and latency, the rule that failed the check named in the
  error (a latency budget breach, an `expect_status` mismatch, a
  body assertion), SKIPPED with the dependency named, MAINTENANCE
  with the window — and exit 0 when all checked endpoints are up
  or in maintenance, 1 when any is down. Nothing is persisted,
  no alert fires, and no dashboard starts, so it is safe to run
  from a deploy script or CI. A single failed check is DOWN here:
  the flap threshold needs repeated rounds and does not apply to
  a one-shot pass. The retry rule does apply: a first-attempt
  failure is retried once, and the line prints "(retried once)"
  when a retry happened, so the dry run judges the same outcome
  the monitor would record.
- Per-endpoint check intervals (`check_interval_s`): an endpoint
  can define its own cadence (e.g. every 10 s for a flaky
  dependency, every 10 minutes for a slow-changing page) instead
  of the global `interval_seconds`. The scheduler checks each
  endpoint when it is due and sleeps until the next one is, so a
  fast endpoint does not wait on a slow one and a slow one is not
  checked every round. Set in the config file or with
  `--check-interval NAME=SECONDS` (repeatable, flag wins); a bad
  value is rejected at config load, and a SIGHUP reload picks up
  changes. A dependent that is due while its dependency is not
  due this round is still judged against the dependency's last
  recorded check. The dashboard gains a Check every column and
  `GET /api/status` gains `check_interval_s` per endpoint (or
  `null` when the endpoint uses the global interval).
- TCP connect checks for services without HTTP.
- Per-endpoint uptime % and recent check history.
- Flap detection: an endpoint only flips to DOWN (and only alerts)
  after 3 consecutive failed checks by default — a single blip is
  logged, not paged. The threshold is configurable per config, and
  each endpoint can set its own `alert_threshold` to page later
  than it flips (a noisy endpoint that flips DOWN at 3 failures
  can wait until 5 before it pages; one that recovers before its
  threshold never pages at all). An endpoint that stays down can
  page again every `alert_cooldown_s` seconds (off by default), so
  a long outage re-pages instead of going quiet; a recovery clears
  the cooldown, so the next outage pages immediately.
- Retry once before a failure counts: when a check fails it is
  immediately retried once, and only a second consecutive failure
  counts as DOWN for flap, alert, and uptime purposes — a blip
  that recovers on the retry is recorded as UP, with the retry
  noted in the history, on the dashboard, and in `GET /api/status`.
  Retries are on by default and can be disabled globally or per
  endpoint (`retry_on_failure`), or with `--no-retry-on-failure`.
- Maintenance windows: an endpoint can declare recurring weekly
  windows (`maintenance_windows`) during which it is not checked at
  all — no alert fires, no flap state changes, and uptime is
  untouched. The dashboard shows MAINTENANCE and lists the windows.
- Uptime % over the last 24 hours, 7 days, and 30 days, plus total
  downtime in each window, so a recent outage shows up even when
  the all-time figure still looks fine. `GET /api/uptime` returns
  the same summary per endpoint as JSON, with the endpoint's
  `sla_target` compared against the 30-day figure; `GET
  /api/status` carries the 30-day uptime and the downtime per
  window too. `--uptime-report` prints the same summary as plain
  text (with `--uptime-out PATH` to write it to a file instead),
  so it can go into a log or a script. `--downtime-report` prints
  the same downtime figures on their own — per endpoint, plus a
  total across endpoints (with `--downtime-out PATH` to write it
  to a file instead) — so an operator can see the monitor's whole
  outage load at a glance.
- Down since: when an endpoint is DOWN the dashboard shows how long
  the outage has lasted ("12m 34s"), from the first failed check in
  the current run of failures — unconfirmed failures below the flap
  threshold count, so the clock starts when it first failed, not
  when the outage was confirmed. The same start time is in
  `GET /api/status` (`down_since`, epoch seconds, `null` when not
  down) and in `GET /metrics` as a `healthcheck_down_seconds` gauge
  (0 when up).
- A static status page: `python3 -m healthcheck.cli --config config.json --status-page status.html`
  renders the same view as the dashboard — current status, how long
  the current outage has lasted, uptime
  24h/7d/30d, latency sparkline, latency p50 / p95 / p99 over the
  last 24 hours, and the Latency budget, Check every, and Retries
  columns after Latency, and the Alert at / Cooldown
  columns next to the Fail streak, plus the Method, Headers, HTTP
  status, and Cert expires columns after the Cooldown column, and
  Depends on / Maintenance columns after Cert expires, and
  a Last error column after the latency sparkline, and an SLA breach
  column after Last error, and a Downtime column right after
  Uptime 30d — plus
  a recent-incidents list per
  endpoint, as one self-contained HTML file with no external
  references, so it can be hosted anywhere with no server. It is a
  snapshot of the persisted history; regenerate it to update it.
  The Alert at column is the endpoint's own `alert_threshold`
  (the flap threshold in effect when it sets none) and the
  Cooldown column is its `alert_cooldown_s` (`-` when it never
  re-pages); a snapshot carries no alert records, so unlike the
  live dashboard it shows the configured cooldown rather than
  the time until the next re-page while an endpoint is DOWN.
  The Down since column is the same figure the dashboard shows,
  from the same `History.down_since()` — the duration from the
  first failed check in the current run of failures (unconfirmed
  failures included) to the page's generated time, `-` when the
  endpoint is not DOWN.
  The Method column is the HTTP method the endpoint is checked
  with (`-` when it uses the default GET, and for TCP endpoints);
  Headers lists the names of the extra request headers the check
  sends, never the values (`-` when it sends none); HTTP status is
  `expected 204, got 200` when the endpoint asserts on a code, the
  bare actual code when it does not, `-` when no status was ever
  returned; Cert expires is whole days until the TLS certificate
  expires (`89d`, `12d (soon)` inside the warning window,
  `expired` once it has lapsed, `-` when no certificate was ever
  seen). All four come from the same helpers the dashboard reads,
  so the two pages can never disagree; a snapshot carries no alert
  records, so unlike the live dashboard the page does not append
  the time a cert-expiry alert fired.
  The Latency budget column is the endpoint's `max_latency_ms`
  (`-` when it has none), Check every is its `check_interval_s`
  (`-` when it uses the global interval), and Retries is the
  retries spent on the most recent recorded check (`-` when never
  checked). All three come from the same helpers the dashboard
  reads, so the two pages can never disagree; a remote source
  shows `-` for all three, since its snapshot carries no endpoint
  config and no per-check records.
  The Last error column is the error string of the endpoint's
  most recent failed check that actually ran (`-` when it never
  failed), from the same helper the dashboard reads, so the two
  pages can never disagree; it stays after a later success, so a
  recovered endpoint still shows why it last failed, and a remote
  source shows `-`, since its snapshot carries no per-check
  records.
  The SLA breach column is the same `BREACH -X.Xpp` figure the
  dashboard shows, right after Last error — from the same helper
  the dashboard reads, so the two pages can never disagree.
  `BREACH -X.Xpp` when the endpoint is below its `sla_target` in
  the page's calendar month, `-` when it has no target, no data
  in the month, or meets the target. A snapshot carries no alert
  records, so unlike the live dashboard it does not append the
  time the breach alert fired; an extra source's endpoints carry
  no config, so they have no target and always show `-`.
  The Downtime column is the total outage time in the last 30
  days, right after Uptime 30d — the same window that column
  covers — from the same `endpoint_downtime_text` the API's
  `downtime_30d_seconds` is built on, so the page can never
  disagree with it. `1h 23m`, `-` when the endpoint recorded no
  downtime in the window (never down, or never checked). An
  outage that started before the window only counts the part
  inside it, and an ongoing outage accrues to the page's
  generated time. A remote source shows `-`, since its snapshot
  carries no per-check records; a file source shows its recorded
  downtime, like a local one.
  The Depends on column is the name from `depends_on` (`-` when
  the endpoint has no dependency) and Maintenance is the
  endpoint's declared windows (`Sat 02:00–04:00`, several joined
  with `; `, `-` when it declares none), from the same helpers the
  dashboard reads, so the two pages can never disagree; an extra
  source's endpoints carry no config, so they show `-` for both.
  Combine with `--once` to check first, then render.
  `--status-page-source LABEL=PATH` (repeatable) merges another
  healthcheck instance's persisted history into the same page under
  its own labelled section, with the same rules per source.
  `LABEL=URL` does the same for a remote instance, scraping its
  `GET /api/status` over HTTP — no local history file needed for
  the second target. Sources can also live in the config file
  (`status_page_sources`, one entry per source: `label` plus
  `path` or `url`), so they survive without repeating the flag
  every time; a flag with the same label as a config entry
  replaces it, and a new label is added. The dashboard and `GET
  /api/status` list the merged sources, so the page's inputs are
  visible while the monitor runs. The remote section shows the remote instance's
  status and uptime figures (all-time, 24h, 7d) as reported; a
  snapshot carries no per-check records, so there is no latency
  sparkline and no incident list for a remote source, and the
  Method, Headers, HTTP status, Cert expires, Depends on, and
  Maintenance columns all show
  `-` there (a file source still shows its recorded HTTP status
  and cert expiry, but `-` for Method, Headers, Depends on, and
  Maintenance, since a history file carries no endpoint config), and an
  unreachable instance (or bad JSON) logs a warning and renders
  empty rather than failing the page.
  An endpoint whose dependency is not up shows SKIPPED, with the
  dependency named — a skip is not downtime.
- Prometheus metrics export: `GET /metrics` on the dashboard server
  emits the same live status in Prometheus text format — a per-endpoint
  up/down gauge, uptime ratios (all-time, 24h, 7d), a latency summary
  with p50/p95/p99 plus sum and count over the last 24 hours, and
  check totals by result — so a Prometheus server can scrape it.
- TLS certificate expiry: every HTTPS check reads the peer
  certificate's expiry (one extra short TLS handshake — urllib
  does not expose the socket it used) and records it with the
  check, so the dashboard, `GET /api/status`, and `GET /metrics`
  can warn before the cert lapses. An expiring cert is a warning,
  not downtime: the endpoint stays UP, and the dashboard shows
  whole days left ("89d", "12d (soon)" inside the endpoint's
  warning window, "expired" once it has lapsed). An endpoint can
  set `cert_expiry_warn_days` (default 30) to choose its own
  window; a check that cannot read the peer certificate reports
  no expiry instead of failing. When a certificate enters its
  warning window, one webhook alert goes out with the expiry
  details (see `webhook_url` below — an endpoint's own `webhook`
  wins) — once per certificate, so later checks while the same
  cert is still inside the window do not re-fire, and a renewed
  cert fires again when it enters the window; the alert and when
  it fired show on the dashboard and in `GET /api/status`.
- A JSON API: `GET /api/status` on the dashboard server returns the
  same data as JSON (status, uptime all-time / 24h / 7d, latency,
  last check, recent incidents and their duration histogram,
  dependency, maintenance windows per endpoint), so scripts and other tools can consume the live status
  without scraping the HTML. `GET /api/incidents` returns just the
  recent incidents per endpoint (newest first, at most 5, an ongoing
  incident's duration running to now), each endpoint's row also
  carrying its per-incident duration histogram over those same
  incidents — `?endpoint=NAME` filters to one endpoint and
  `?limit=N` caps the incidents per endpoint.
- SLA reporting: an endpoint can declare an `sla_target` (e.g.
  99.9); `python3 -m healthcheck.cli --config config.json
  --sla-report 2026-11` prints that calendar month's uptime per
  endpoint against its target, with the breach list — endpoints
  that fell short, and by how many percentage points.
  `--sla-format csv` / `json` exports the same month as CSV or
  JSON instead (with `--sla-out PATH` to write it to a file),
  adding per-endpoint incident counts, downtime in the month,
  and latency p50/p95/p99 over the month, so the report can go
  into a spreadsheet or a script. `GET /api/sla` on the
  dashboard server serves the same JSON live (current month,
  or `?month=YYYY-MM` for another month). While the monitor
  runs, a breach is also alerted: the first time a month's report
  shows an endpoint below its target, one webhook POST goes out
  with the breach details (see `webhook_url` below — an
  endpoint's own `webhook` wins), so the breach is paged while
  the month is still running, not only when someone prints the
  report; the breach and when it was alerted show on the
  dashboard and in `GET /api/status`. `--sla-alert [YYYY-MM]`
  fires the same alerts once from the command line and exits,
  for a cron job or a manual check.
- Config hot-reload: while the monitor is running, edit
  `config.json` and send SIGHUP (`kill -HUP <pid>`); the new
  endpoints, check interval, per-endpoint check intervals,
  webhook, flap threshold, alert thresholds, and status
  page sources take effect before the next check round, and the
  dashboard follows.
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

Dry-run mode: validate the config, check every endpoint once (or
just one, with `--check NAME`), print the verdict per endpoint, and
exit 0 when all checked endpoints are up, 1 when any is down — no
history is written, no alert fires, no dashboard starts:

```sh
python3 -m healthcheck.cli --config config.json --check
python3 -m healthcheck.cli --config config.json --check api
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
kill -HUP <pid>   # new endpoints, interval, per-endpoint check
                  # intervals, webhook, flap threshold, alert
                  # thresholds, and status page sources apply
                  # before the next check round
```

The reloaded config is validated the same way as at startup: a
missing file, bad JSON, or an invalid endpoint (unknown
dependency, bad `sla_target`, bad regex, ...) is logged as a
warning and the running config is kept — the monitor never stops
on a bad edit. Endpoints added by the reload are checked from the
next round, removed ones stop being checked, and the dashboard
and `GET /api/status` show the new endpoint list. A reload also
picks up changes to `status_page_sources`; a bad source entry in
the reloaded file is warned about and skipped (the rest of the
new config still applies), it does not keep the old config. Two things do
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
- `check_interval_s` — seconds between checks of one endpoint
  (optional, either type, e.g. `60`): the endpoint is checked on
  its own cadence instead of the global `interval_seconds`, which
  stays the default for endpoints without one. The scheduler
  checks each endpoint when it is due and sleeps until the next
  one is due, so a fast endpoint is not held back by a slow one.
  Must be a positive number (an integer or a float); a bad value
  is rejected at config load. The `--check-interval NAME=SECONDS`
  flag (repeatable) sets or replaces it for one endpoint, and the
  flag wins over the config file; a flag naming an endpoint that
  is not in the config is rejected. A SIGHUP reload picks up
  `check_interval_s` changes from the edited file (a bad value
  keeps the running config), and re-applies the flags.
- `flap_threshold` — consecutive failed checks that confirm an
  outage (default 3). A failure below the threshold is recorded but
  unconfirmed: no alert, no uptime penalty, and the endpoint stays
  UP on the dashboard. A success at any point resets the count; a
  success after a confirmed outage is a recovery. Must be a
  positive integer. The `--flap-threshold` flag overrides it.
  The flap threshold also decides up/down state only — when the
  outage pages is the endpoint's `alert_threshold` (below), which
  defaults to this threshold.
- `alert_threshold` — consecutive failed checks before an
  endpoint's outage pages (optional, either type, e.g. `5`): an
  endpoint with `alert_threshold` 5 and the default flap
  threshold 3 flips DOWN on the third failure but only alerts on
  the fifth — a noisy endpoint can page later than it flips, and
  one that recovers before the fifth never pages at all (and no
  recovery alert follows, since it never paged). A value at or
  below the flap threshold alerts at confirmation, like before.
  Must be a positive integer; a bad value is rejected at config
  load. The `--alert-threshold NAME=N` flag (repeatable) sets or
  replaces it for one endpoint, and the flag wins over the config
  file; a flag naming an endpoint that is not in the config is
  rejected. A SIGHUP reload picks up `alert_threshold` changes
  from the edited file (a bad value keeps the running config),
  and re-applies the flags. The dashboard gains an Alert at
  column and `GET /api/status` gains `alert_threshold` per
  endpoint, next to the Fail streak / `consecutive_failures` they
  are read against.
- `alert_cooldown_s` — seconds between pages for an endpoint's
  ongoing outage (optional, either type, e.g. `300`): an endpoint
  that stays down pages again once that many seconds have passed
  since its last down alert, so a long outage re-pages instead of
  going quiet after the first page — the repeat page carries
  `"repeat": true` in its webhook payload, so the receiver can
  tell it from the first page of the outage. The first page of an
  outage is immediate (the cooldown only spaces repeats of the
  same outage, never a new outage's first page), and a recovery
  clears the cooldown state, so the next outage pages immediately
  and the recovery alert itself is never delayed. An endpoint
  without one never re-pages — it alerts exactly once per outage,
  like before. It can also be set globally in the config
  (`alert_cooldown_s` at the top level) as the default for
  endpoints without their own; a per-endpoint value wins over the
  global. Must be a positive number of seconds; a bad value is
  rejected at config load. The `--alert-cooldown NAME=SECONDS`
  flag (repeatable) sets or replaces it for one endpoint, and the
  flag wins over the config file; a flag naming an endpoint that
  is not in the config is rejected. A SIGHUP reload picks up
  `alert_cooldown_s` changes from the edited file (a bad value
  keeps the running config), and re-applies the flags — the reload
  does reset flap state, so a mid-outage endpoint re-pages once
  it re-confirms, even inside the cooldown, and the cooldown then
  governs repeats after that page. The dashboard gains a Cooldown
  column and `GET /api/status` gains `alert_cooldown_s`,
  `last_alerted_at`, and `alert_cooldown_remaining_s` per
  endpoint, so an operator can see the next page coming without
  waiting for it.
- `retry_on_failure` — retry a failed check once before it counts
  (default true, either type, or globally in the config). When a
  check fails it is immediately retried once, on the same endpoint
  with the same check; only a second consecutive failure counts as
  DOWN for flap, alert, and uptime purposes. A first-attempt
  failure that recovers on the retry is recorded as UP, with
  `retries: 1` on the check — the retry is part of the same check
  round, not a second check, so one history entry is recorded and
  the endpoint's next-due time (its `check_interval_s` cadence)
  is not reset by it. The retry count is visible per endpoint in
  the check history, on the dashboard (Retries column), and in
  `GET /api/status` (`retries` for the most recent check,
  `retries_total` across the recorded history). Set `false`
  globally to disable retries for every endpoint, or per endpoint
  to disable them for just that one (the per-endpoint value wins);
  the `--no-retry-on-failure` flag disables them for the run. A
  non-bool value is rejected at config load, and a SIGHUP reload
  picks up changes. Dry-run mode (`--check`) applies the same
  rule and prints "(retried once)" on the line when a retry
  happened.
- `history` — checks kept per endpoint (default 100).
- `history_file` — path to a JSON file for persisting check history
  across restarts (optional; default is in-memory only). The
  `--history-file` flag overrides it. The file is written atomically
  after every check round and on shutdown.
- `status_page_sources` — extra sources for the static status page
  (optional), so they survive without repeating
  `--status-page-source` every time: a list of `{"label": ...,
  "path": ...}` entries (another instance's persisted history
  file) and/or `{"label": ..., "url": ...}` entries (a remote
  instance's base URL, scraped over `GET /api/status`). A
  `--status-page-source LABEL=...` flag with the same label as a
  config entry replaces it; a new label is added. A bad entry
  (not an object, no label, both or neither of `path`/`url`, a
  non-http(s) `url`, a duplicate label) is logged as a warning
  and skipped — a bad second source never takes the monitor down.
  The merged list is shown on the dashboard and in `GET
  /api/status`, and a SIGHUP reload picks up changes to it.
- `webhook_url` — URL to POST a JSON alert to when an endpoint changes
  state (optional; default is log lines only). The `--webhook-url`
  flag overrides it. The payload is
  `{"endpoint", "event" ("down" or "recovered"), "ok", "previous_ok",
  "latency_ms", "status_code", "error", "checked_at"}`. The first
  check of an endpoint sets its baseline and sends nothing; a failed
  delivery is logged as a warning and never stops the monitor. With
  flap detection, the alert fires only when the outage is confirmed
  (see `flap_threshold` above), not on a single failed check.
  The same URL also receives certificate-expiry alerts: when an
  endpoint's TLS certificate enters its warning window (its
  `cert_expiry_warn_days`, 30 by default), one POST goes out with
  `{"type": "cert_expiry", "endpoint", "cert_expires_at",
  "days_remaining", "warn_days", "checked_at"}` — the expiry in
  epoch seconds, the whole days left, and the endpoint's warning
  window — to the endpoint's own `webhook` when it has one, else
  the global `webhook_url`. One alert per certificate: later checks
  while the same certificate is still inside the window do not
  re-fire, and a renewed certificate fires again when it enters the
  window; a failed delivery is logged and the alert is still
  recorded as fired, so it is not re-sent. The alert and when it
  fired show on the dashboard and in `GET /api/status`.
  The same URL also receives SLA breach alerts: when a month's SLA
  report shows an endpoint below its `sla_target`, one POST goes
  out per endpoint per month with
  `{"type": "sla_breach", "endpoint", "month", "uptime_pct",
  "checks", "incidents", "downtime_seconds", "target",
  "shortfall_pct", "latency"}` — the first breach in a month fires
  it, and later rounds in the same month do not re-fire, even if the
  shortfall grows (the live shortfall stays visible on the dashboard
  and in `GET /api/status`); a new month starts fresh. A failed
  delivery is logged and the breach is still recorded as alerted,
  like the digest — a failed alert is not re-sent. `--sla-alert
  [YYYY-MM]` fires the same alerts once from the command line and
  exits, for a cron job or a manual check (with no webhook
  configured anywhere, it prints a note and sends nothing).
  The same URL also receives p95 latency breach alerts: when an
  endpoint's windowed p95 over the last 24 hours crosses its
  `latency_p95_threshold_ms`, one POST goes out with
  `{"type": "latency_p95_breach", "endpoint", "p95_ms",
  "threshold_ms", "window_hours", "checked_at"}` — the p95 that
  crossed, the threshold it crossed, the 24-hour window, and the
  most recent check time — to the endpoint's own `webhook` when
  it has one, else the global `webhook_url`. One alert per
  crossing: later rounds while the p95 stays above the threshold
  do not re-fire, and a p95 that falls back below the threshold
  re-arms, so the next crossing fires again; a failed delivery
  is logged and the breach is still recorded as alerted, so it
  is not re-sent. The alert and when it fired show on the
  dashboard and in `GET /api/status`.
  An endpoint can set its own `webhook` URL instead: its
  state-change alerts go to its own channel, and its slice of the
  alert digest is POSTed there too (see `alert_digest` below) —
  endpoints without one fall back to `webhook_url`. A bad
  per-endpoint URL (not an http(s) URL) is rejected at config load.
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
  (flapping). When an endpoint has its own `webhook` URL, its
  changes (and its currently-down entry) go in a separate digest
  POSTed to its own webhook instead, and the digest email still
  covers every endpoint. An empty window — no changes and nothing currently
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
  `method` is the HTTP method the check sends (optional, e.g.
  `HEAD` or `POST`; GET by default): the check sends no body, so
  only `GET`, `HEAD`, `POST`, `PUT`, `DELETE`, `PATCH`, and
  `OPTIONS` are accepted — a method outside that list, a
  non-string value, or the key on a `tcp` endpoint is rejected at
  config load, and the value is normalized to upper case. A HEAD
  check carries no response body, so `expect_body` /
  `expect_body_regex` cannot be combined with it and are rejected
  together; every other method judges the status (and the body
  assertions, when set) exactly like a GET.
  `headers` is a map of extra request headers to send with the
  check (optional, e.g. `{"Authorization": "Bearer ..."}` for a
  health endpoint behind auth, or `{"X-Api-Key": "..."}` for one
  that wants an API key): the values are sent verbatim, exactly
  as written. A non-object value, a non-string header name or
  value, or the key on a `tcp` endpoint is rejected at config
  load. The values are secrets and are never shown: the dashboard
  and `GET /api/status` list only the header names, so an
  operator can see that a check authenticates without the token
  ever leaving the config file.
  `expect_status` is an exact status code the response must have
  (e.g. `204` for a no-content health endpoint): when it is set
  it replaces the 2xx/3xx rule entirely, so a 200 from an endpoint
  that should answer 204 is DOWN, with the expected and actual
  codes named in the error (`expected HTTP 204, got 200`). An
  expected 4xx/5xx status counts too — a 404 that is supposed to
  be a 404 is UP. Body assertions still apply once the status
  matches. The code is checked against the final response status,
  after redirects are followed. A non-integer value, a code
  outside 100–599, or the key on a `tcp` endpoint is rejected at
  config load. The `--expect-status NAME=STATUS` flag (repeatable)
  sets or replaces it for one endpoint, and the flag wins over the
  config file; a flag naming an endpoint that is not in the config
  is rejected. A SIGHUP reload picks up `expect_status` changes
  from the edited file (a bad value keeps the running config),
  and re-applies the flags — a flag naming an endpoint the
  reloaded file dropped keeps the running config too.
- `max_latency_ms` — the slowest an endpoint may answer, in
  milliseconds (optional, either type, e.g. `500`): a check that
  answers slower is DOWN, with the actual latency and the budget
  named in the error (`latency 812.3 ms exceeds budget 500 ms`),
  and the failure goes through the same flap detection, alert,
  and uptime rules as any other failed check — a single slow
  check is unconfirmed, and the endpoint only flips to DOWN
  after the threshold's consecutive slow checks. The slow
  latency is still recorded, so the dashboard and `GET
  /api/status` show how slow it was next to the budget. Must be
  a positive number (an integer or a float); a bad value is
  rejected at config load. The `--max-latency NAME=MS` flag
  (repeatable) sets or replaces it for one endpoint, and the
  flag wins over the config file; a flag naming an endpoint that
  is not in the config is rejected. A SIGHUP reload picks up
  `max_latency_ms` changes from the edited file (a bad value
  keeps the running config), and re-applies the flags.
  `cafile`, `cert`, and `key` are optional: `cafile` is a PEM bundle
  of certificate authorities to trust for an `https://` endpoint
  (for a private or self-signed CA); `cert` and `key` are a client
  certificate and private key for mutual TLS. Without them, an
  `https://` endpoint is verified against the system CA store, as
  before. A bad cert file makes the check fail, never crash. The
  TLS options are ignored for plain `http://` URLs.
- `latency_p95_threshold_ms` — the p95 latency in milliseconds
  (optional, either type, e.g. `400`) above which the endpoint is
  in a latency-alert state: the windowed p95 over the last 24
  hours (the same nearest-rank p95 the dashboard's latency
  percentiles column and `GET /api/status` report) is compared
  against the threshold after every check round, and while it
  sits above it the dashboard's Latency p95 alert column shows
  `ALERT 812.3 > 400 ms` and `GET /api/status` reports
  `latency_p95_alert: true` next to `latency_p95_threshold_ms`.
  The alert is advisory only — the endpoint stays UP, like a
  cert-expiry warning or an SLA breach — and one webhook alert
  fires per crossing when a webhook is configured: the first
  round that finds the p95 above the threshold POSTs a
  `latency_p95_breach` payload (endpoint, p95, threshold, the
  24-hour window, the most recent check time) to the endpoint's
  own `webhook` URL when it has one, else the global
  `webhook_url`; later rounds while the p95 stays above it do
  not re-fire, and the alert re-arms — and the dashboard and
  `GET /api/status` drop the alert time — as soon as a later
  round's p95 falls back inside the threshold, so the next
  crossing fires again. A failed delivery is logged and never
  stops the monitor, and the breach is still recorded as
  alerted, so a failed alert is not re-sent. An endpoint with
  no threshold never alerts, and a threshold with no recorded
  latencies in the window is not a slowdown either. Must be a
  positive number (an integer or a float); a bad value is
  rejected at config load. The `--latency-p95-threshold NAME=MS`
  flag (repeatable) sets or replaces it for one endpoint, and
  the flag wins over the config file; a flag naming an endpoint
  that is not in the config is rejected. A SIGHUP reload picks
  up `latency_p95_threshold_ms` changes from the edited file (a
  bad value keeps the running config), and re-applies the flags.
- `cert_expiry_warn_days` — how many days before an `https://`
  endpoint's TLS certificate expires the monitor starts warning
  about it (optional, either type, e.g. `14`): the dashboard's
  Cert expires column shows "(soon)" inside the window, `GET
  /api/status` reports `cert_expiry_warning: true`, and the
  expiry is still only a warning — the endpoint stays UP, since
  the certificate has not lapsed yet. The default is 30 days.
  Must be a positive number (an integer or a float); a bad value
  is rejected at config load, and the key on a `tcp` endpoint is
  rejected too — a TCP check has no certificate to warn about.
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
(UP / DOWN / SKIPPED / MAINTENANCE / UNKNOWN), how long the current
outage has lasted (the Down since column — a duration like "12m 34s"
when DOWN, `-` otherwise, counted from the first failed check in the
current run of failures, unconfirmed ones included), how many failed
checks in a row the endpoint has right now (the Fail streak column —
`0` when it is up or was never checked, so a still-UP endpoint with
1 or 2 shows how close it is to the flap threshold confirming it
down; it is the same run Down since starts from, so the two can
never disagree), and how many failures in a row it takes before the
endpoint pages (the Alert at column — the endpoint's own
`alert_threshold`, or the flap threshold, 3 by default, when it
sets none — so the streak next to it says how close the endpoint is
to paging: the endpoint flips DOWN at the flap threshold, but the
alert only fires once the streak reaches Alert at), the seconds
between pages while the endpoint's outage continues (the Cooldown
column — the endpoint's `alert_cooldown_s`, `-` when it never
re-pages and alerts exactly once per outage — with the time until
the next page appended while it is DOWN and a re-page is pending,
e.g. "300 s (re-alert in 4m 12s)", or "300 s (re-alert due)" once
the cooldown has elapsed; a recovery clears the record, so a
recovered endpoint shows just the configured cooldown), the HTTP method
the endpoint is checked with (`HEAD`, `POST`, `-` when it uses the
default GET — TCP endpoints show `-` too), the names of the extra
request headers the check sends (`Authorization`, `X-Api-Key`, `-`
when it sends none — the values are never shown, since a value is
often a secret), the HTTP status
(`expected 204, got 200` when the endpoint asserts on a code, the
bare actual code when it does not, `-` when no status was ever
returned — TCP endpoints, never-checked, skipped, maintenance),
when the endpoint's TLS certificate expires (whole days left —
"89d", "12d (soon)" inside the endpoint's `cert_expiry_warn_days`
window, "expired" once it has lapsed, `-` when no certificate was
ever seen — plain http / tcp, never-checked, or a check that could
not read the peer certificate — with the time the cert-expiry alert
fired appended when one went out for the current certificate,
e.g. "5d (soon) alerted 06:12"),
the endpoint it depends on (or `-`), its maintenance windows (or `-`), uptime % over the
recorded history, uptime %
over the last 24 hours, the last 7 days, and the last 30 days (a `-` when no checks fall
in that window — no data is not downtime), total downtime in the
last 30 days (the Downtime 30d column — `1h 23m`, `-` when the
endpoint recorded no downtime in the window), most recent
latency, the endpoint's latency budget (`max_latency_ms`, or `-`
when it has none), the endpoint's own check interval
(`check_interval_s`, or `-` when it uses the global interval),
a latency p50 / p95 / p99 column (nearest-rank percentiles
over the last 24 hours, `-` when no checks ran in that window), a Latency p95 alert column
(`ALERT 812.3 > 400 ms` when that p95 crosses the endpoint's
`latency_p95_threshold_ms`, with the time its breach alert fired
appended when one went out, `p95 210.0 / 400 ms` when it is
inside it, `-` when the endpoint has no threshold — advisory
only, the endpoint stays UP), a sparkline
of the last 20 recorded latencies, a Retries column (retries spent
on the most recent recorded check — `1` when a first-attempt
failure recovered on the retry, `0` when the first attempt decided
it, `-` when never checked), the error string of the endpoint's
most recent failed check (the Last error column, `-` when it never
failed — it stays after a later success, so a recovered endpoint
still shows why it last failed), an SLA breach column (`BREACH
-X.Xpp` when the endpoint is below its `sla_target` in the current
calendar month, with the time its breach alert fired when one went
out, `-` otherwise — no target, no data, or meeting the target),
and last check
time. Skipped checks are recorded in history with the dependency that
caused the skip, but they never count in uptime and never appear in
the sparkline or the percentiles; maintenance rounds are excluded the same way. A failure below the flap threshold likewise never
counts in uptime, and the endpoint still shows UP — it only flips to
DOWN once the threshold of consecutive failures is reached. Below the
table, a "Recent incidents" section lists each endpoint's recent
outages, newest first, at most 5 per endpoint — the same incidents
`GET /api/status` reports, from the same shared helper, so the
dashboard, the API, and the static status page can never disagree.
An ongoing incident shows "ongoing" instead of an end time, with its
duration running to now; an endpoint with no incidents gets no
subsection, and with no incidents recorded anywhere the section is
omitted entirely. Each endpoint's subsection also shows its
duration histogram — the same 7 log-spaced buckets the
`--incidents` text report prints (`< 1m`, `1m – 5m`, `5m – 15m`,
`15m – 1h`, `1h – 6h`, `6h – 24h`, `> 24h`), from
`History.incident_duration_histogram()` with the same limit of 5,
so the histogram can never disagree with the incidents listed
above it. Any other
path returns 404.

`GET /api/status` returns the same data as JSON, for scripts and
other tools: `{"generated_at": <epoch>, "endpoints": [...]}` with one
object per endpoint — `name`, `status` (UP / DOWN / SKIPPED /
MAINTENANCE / UNKNOWN, same rules as the dashboard), `depends_on`
(name or `null`), `maintenance_windows` (list of `start` / `end` /
`days`), `expect_status` (the exact status the endpoint must
return, or `null` when it accepts any 2xx/3xx), `method` (the HTTP
method the endpoint is checked with, or `null` when it uses the
default GET — `null` for `tcp` endpoints too), `headers` (the names of the extra
request headers the endpoint sends with its checks, sorted, or `null` when it
sends none — the values are never exposed, since a header value is often a
secret like an Authorization token), `status_code` (the
most recent actual HTTP status, or `null` when none was ever
returned), `cert_expires_at` (the epoch second the endpoint's TLS
certificate expires, or `null` when no certificate was ever seen),
`cert_expiry_days` (whole days until that expiry, negative when
expired, `null` when unknown), `cert_expiry_warn_days` (the
endpoint's warning window in days, 30 by default), and
`cert_expiry_warning` (whether the certificate expires inside that
window — advisory only, it never changes the endpoint's status),
`cert_expiry_alerted_at` (the epoch second the cert-expiry alert
fired for the endpoint's current certificate, or `null` when it has
not fired — a record for a different, renewed certificate does not
count),
`uptime_pct`, `uptime_24h_pct`, `uptime_7d_pct` (`null` when
no checks fall in that window), `latency_ms` (most recent, or `null`),
`max_latency_ms` (the latency budget in ms the endpoint must answer
within, or `null` when it has none),
`check_interval_s` (the endpoint's own check interval in seconds,
or `null` when it uses the global `interval_seconds`),
`retries` (retries spent on the most recent recorded check — `0`
when the first attempt decided it, `1` when a first-attempt failure
recovered on the retry — or `null` when never checked) and
`retries_total` (retries spent across the recorded history),
`latency_percentiles` (`{"p50", "p95", "p99"}` in ms over the
last 24 hours, or `null` when no checks ran in that window),
`latency_p95_threshold_ms` (the p95 threshold in ms the
endpoint's windowed p95 must stay within, or `null` when it has
none) and `latency_p95_alert` (`true` when that p95 exceeds the
threshold — advisory only, the endpoint stays UP — `false` when
it has no threshold, no recorded latencies, or the p95 is inside
the threshold) and `latency_p95_alerted_at` (the epoch second the
p95 breach alert fired for the current crossing, or `null` when
it has not fired or the p95 has since fallen back below its
threshold),
`last_error` (the error string of the most recent failed check
that actually ran, or `null` when the endpoint never failed —
skipped and maintenance rounds do not count, and it stays after
a later success), `sla_target` (the endpoint's configured SLA
target, or `null`), `sla_breach` (whether the endpoint is below
its target in the current calendar month — `false` when it meets
it or has no target), `sla_shortfall_pct` (how far short it fell,
or `null` when it did not breach), `sla_breach_alerted_at` (the
epoch second its breach alert fired this month, or `null` when
it has not fired — the same computation as `--sla-report`, so the
live status and the report can never disagree), `down_since` (the
epoch second the current outage started — the first failed check
in the current run of failures, unconfirmed ones included — or
`null` when the endpoint is not currently down), `consecutive_failures`
(how many failed checks in a row the endpoint has right now — `0`
when it is up or was never checked — the same run `down_since`
starts, so the count and the outage clock can never disagree;
read it against the endpoint's `alert_threshold` to see how close
a still-UP endpoint is to paging — the endpoint flips DOWN at the
flap threshold, 3 by default, but the alert only fires once the
streak reaches `alert_threshold`, which is the endpoint's own
value when set and the flap threshold otherwise), `alert_threshold`
(the consecutive failures before the endpoint's outage pages — its
own `alert_threshold` when set, else the flap threshold in effect),
`alert_cooldown_s` (the seconds between pages for the endpoint's
ongoing outage — its own `alert_cooldown_s`, the config's global
one, or `null` when it never re-pages), `last_alerted_at` (the
epoch second the endpoint's current outage last paged, or `null`
when it never paged or has recovered since), `alert_cooldown_remaining_s`
(how many seconds until it pages again while it stays DOWN — `0`
when the cooldown has already elapsed, `null` when it is not down,
has no cooldown, or never paged),
`recent_incidents`
(the endpoint's recent outages, newest first, at most 5 — each
`{"start", "end", "checks", "ongoing", "duration_s"}`, grouped the
same way as the static status page's incident list, so the API and
the page can never disagree; an ongoing incident has `end` `null`
and its duration runs to the snapshot time), `duration_histogram`
(those same incidents bucketed by duration into the 7 log-spaced
buckets the `--incidents` text report prints — each `{"label",
"min_s", "max_s", "count"}`, all 7 buckets present even at 0, so
an endpoint with no incidents gets all zeroes and the histogram
can never disagree with the incidents listed above it), and
`last_checked_at` (epoch seconds, or `null` when never checked) —
plus a `status_page_sources` list (`{"label", "path"|"url"}` per
merged source), so a consumer can see what the static status page
would merge in. The dashboard shows the same list in a "Status
page sources" section below the endpoint table.

`GET /metrics` returns the same data in Prometheus text format, for
scraping: `healthcheck_up` (gauge, 1 when the endpoint is currently
UP, 0 when DOWN — omitted for a never-checked, SKIPPED, or
maintenance endpoint), `healthcheck_down_seconds` (gauge, seconds
the endpoint has been down — 0 when up, counted from the first
failed check in the current run, unconfirmed ones included;
omitted for a never-checked, SKIPPED, or maintenance endpoint),
`healthcheck_uptime_ratio` (gauge, share of
checks that were up, all-time) plus `healthcheck_uptime_ratio_24h`,
`healthcheck_uptime_ratio_7d`, and `healthcheck_uptime_ratio_30d`
(the 30-day figure is the same one `GET /api/uptime` reports as
`uptime_30d_pct`, so the gauge and the API can never disagree),
`healthcheck_downtime_seconds_24h`, `healthcheck_downtime_seconds_7d`,
and `healthcheck_downtime_seconds_30d` (gauges, total outage time in
seconds inside the last 24 hours / 7 days / 30 days — the same
figures `GET /api/status` reports as `downtime_24h_seconds` /
`downtime_7d_seconds` / `downtime_30d_seconds`, so the gauges and
the API can never disagree; always present, even at 0, so a
Prometheus server can alert when the 30-day downtime passes a
budget),
(gauge, whole days until the endpoint's TLS certificate expires —
negative when expired, omitted when no expiry was recorded),
`healthcheck_latency_ms` (summary
with `quantile="0.5" / "0.95" / "0.99"` in ms over the last 24 hours,
plus `_sum` and `_count`), `healthcheck_checks_total` (counter,
split by `result="success"` / `"failure"`), and
`healthcheck_incidents_total` (counter, total recorded incidents
per endpoint — runs of consecutive confirmed-down checks — from
the same grouping the incident lists use, so the counter and the
lists can never disagree; always present, even at 0), and
`healthcheck_consecutive_failures` (gauge, how many failed
checks in a row the endpoint has right now — the same run
`down_since` starts from, so the gauge and the outage clock can
never disagree; always present, even at 0, so a Prometheus
server can alert when it reaches the endpoint's `alert_threshold`
— the flap threshold, 3 by default, when the endpoint sets none). The same
exclusion rules apply as everywhere else: skipped and maintenance
checks never ran, so they are omitted from the up gauge and the
totals and carry no latency; a failure below the flap threshold
still shows UP and counts as a failure in the totals, since the
check did fail. An endpoint with no counted data in a window has
no series for that window — no data is not downtime.

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

Export the same month as CSV or JSON instead, for a spreadsheet
or a script — the export adds per-endpoint incident counts,
downtime inside the month, and latency p50/p95/p99 over the
month (an incident is a run of consecutive confirmed-down
checks, like the status page's incident list, and its downtime
runs from the first down check to the first passing check after
it, clipped to the month; an incident still open at the month's
end accrues downtime to the month end):

```sh
python3 -m healthcheck.cli --config config.json --sla-report 2026-11 --sla-format csv
python3 -m healthcheck.cli --config config.json --sla-report 2026-11 --sla-format json --sla-out sla-2026-11.json
```

The CSV has one row per endpoint (`month, endpoint,
uptime_pct, checks, failed_checks, incidents, downtime_seconds,
latency_p50_ms, latency_p95_ms, latency_p99_ms, sla_target, met,
shortfall_pct`); the JSON is `{"month", "endpoints": [...],
"breaches": [...]}` with the same fields per endpoint.

The same JSON is also served live while the monitor runs:
`GET /api/sla` on the dashboard server returns the current
month's report, and `GET /api/sla?month=2026-11` a past month
(a bad month is a 400 with a JSON error). It is built from the
same computation as the export, so the two can never disagree.

A breach is also alerted, not just reported: while the monitor
runs, each check round re-evaluates the current month, and the
first time an endpoint falls below its target one webhook POST
goes out with the breach details (see `webhook_url` above — an
endpoint's own `webhook` wins, and with no webhook configured
nothing is sent). The breach and when it was alerted show on the
dashboard and in `GET /api/status`, and `--sla-alert [YYYY-MM]`
fires the same alerts once from the command line and exits, for
a cron job or a manual check.

## Incident export

`--incidents` exports the recorded incident history from the
persisted history (needs `history_file` in the config, or
`--history-file`) — every recorded outage per endpoint, newest
first, with per-endpoint incident counts and total downtime,
and totals across endpoints. An ongoing incident has `end`
`null` and its duration running to the export time. The export
is built from the same `History.recent_incidents()` the
dashboard, `GET /api/status`, `GET /api/incidents`, and the
static status page use, so it can never disagree with them.

```sh
python3 -m healthcheck.cli --config config.json --incidents
```

```
Incident report (generated 2026-11-24 09:01:11)
  api: 2 incidents (1 ongoing), 334661.1s total downtime
    2026-11-20 12:05:00 – ongoing (334571.1s, 1 check)
    2026-11-20 12:01:00 – 2026-11-20 12:02:30 (90.0s, 1 check)
2 incidents total (1 ongoing), 334661.1s total downtime
```

`--incidents-format csv` / `json` exports the same incidents
instead (with `--incidents-out PATH` to write it to a file).
`--endpoint NAME` exports only that endpoint's incidents and
`--limit N` caps the incidents per endpoint, newest first —
the same filters `GET /api/incidents` takes as `?endpoint=` and
`?limit=`, and an unknown endpoint or a non-positive limit is
an error (exit 2), not an empty report, so a typo does not
silently export nothing. The totals always sum the rows
actually exported, so a filtered export's totals are
consistent with its own rows.
The CSV has one row per incident (`endpoint, start, end,
ongoing, duration_s, checks`, with `end` empty for an ongoing
incident); the JSON is `{"generated_at", "endpoints": [...],
"totals": {...}}` with the full incident list and the
per-endpoint counts (`total_incidents`, `ongoing_incidents`,
`total_downtime_seconds`) plus the totals across endpoints. The
text report also prints a per-endpoint duration histogram after
each endpoint's incidents — log-spaced buckets over the same
incidents (`< 1m`, `1m – 5m`, `5m – 15m`, `15m – 1h`, `1h – 6h`,
`6h – 24h`, `> 24h`), so a glance shows whether outages are
mostly blips or mostly long ones; the same histogram is in each
JSON row (`duration_histogram`). An endpoint with no incidents
gets no histogram, and a filtered export's histogram covers only
the incidents actually exported.

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
- [x] Per-endpoint alert routing: an endpoint can set its own
  `webhook` URL, so its state-change alerts go to its own channel
  and its slice of the alert digest is POSTed there, while other
  endpoints fall back to the global `webhook_url` (or the digest
  block's `webhook_url`); a bad per-endpoint URL is rejected at
  config load — done in 0.19.0.
- [x] Second scrape target for the static status page: `--status-page-source LABEL=PATH`
  (repeatable) merges another healthcheck instance's persisted
  history file into the same `--status-page` output, under its own
  labelled section (the local history is labelled `local`), with
  the same status, uptime and incident-grouping rules per source —
  an endpoint with the same name in both sources is judged
  separately in each. A missing or corrupt second history logs a
  warning and renders empty instead of taking the page down —
  done in 0.20.0.
- [x] Scrape a remote healthcheck instance's `GET /api/status`
  over HTTP as a status-page source: `--status-page-source
  LABEL=URL` fetches the remote instance's status snapshot and
  renders it as a labelled section with the same status and uptime
  rules — no local history file needed for the second target. A
  snapshot has no per-check records, so a remote source has no
  latency sparkline and no incident list; an unreachable instance
  or bad JSON logs a warning and renders empty instead of taking
  the page down — done in 0.21.0.
- [x] `status_page_sources` in the config file: `{"label", "path"}`
  and `{"label", "url"}` entries so file and remote sources
  survive without repeating the flag every time, validated at
  load and on SIGHUP reload (a bad entry is warned about and
  skipped, never fatal); `--status-page-source` flags still work
  and replace a config entry with the same label. The dashboard
  and `GET /api/status` list the merged sources — done in
  0.22.0.
- [x] Per-endpoint expected HTTP status (`expect_status`,
  e.g. 204 for a health endpoint that never returns 200), so a
  check can assert on the exact status code instead of only
  accepting any 2xx/3xx. A mismatch is DOWN, with the expected
  and actual codes named, and goes through the same flap and
  alert rules as any other failure; an expected 4xx/5xx counts
  too. Set in the config file or with `--expect-status
  NAME=STATUS` (repeatable, flag wins), validated at load and
  on SIGHUP reload; the dashboard gains an HTTP status column
  and `GET /api/status` gains `expect_status` / `status_code`
  per endpoint — done in 0.23.0.
- [x] Per-endpoint latency budgets (`max_latency_ms`): a check
  that answers slower than the endpoint's budget is DOWN, with
  the actual latency and the budget named in the error, and goes
  through the same flap detection, alert, and uptime rules as any
  other failure; the slow latency is still recorded. Set in the
  config file or with `--max-latency NAME=MS` (repeatable, flag
  wins), validated at load and on SIGHUP reload; the dashboard
  gains a Latency budget column and `GET /api/status` gains
  `max_latency_ms` per endpoint — done in 0.24.0.
- [x] Dry-run mode (`--check [NAME]`): validate the config, run
  one check pass over every endpoint (or just the named one, with
  its dependencies), print the per-endpoint verdict (UP/DOWN with
  status and latency, the rule that failed the check named, SKIPPED
  with the dependency, MAINTENANCE with the window), and exit 0
  when all checked endpoints are up, 1 when any is down — no
  history written, no alert fired, no dashboard started, so it is
  safe from a deploy script or CI. A single failed check is DOWN
  (the flap threshold needs repeated rounds and does not apply to
  a one-shot pass); an unknown name is rejected at exit code 2 —
  done in 0.25.0.
- [x] Last error per endpoint: `History.last_error()` returns the
  error string of the endpoint's most recent failed check that
  actually ran (skipped and maintenance rounds do not count, and
  the error stays after a later success, so a recovered endpoint
  still shows why it last failed). The dashboard gains a Last
  error column (HTML-escaped, `-` when the endpoint never failed)
  and `GET /api/status` gains `last_error` per endpoint — done
  in 0.26.0.
- [x] Per-endpoint check intervals (`check_interval_s`): an
  endpoint can define its own check cadence in seconds instead of
  the global `interval_seconds`, which stays the default for
  endpoints without one. The scheduler checks each endpoint when
  it is due and sleeps until the next one is due, so a fast
  endpoint does not wait on a slow one; a dependent due while its
  dependency is not due this round is judged against the
  dependency's last recorded check. Set in the config file or
  with `--check-interval NAME=SECONDS` (repeatable, flag wins),
  validated at load and on SIGHUP reload; the dashboard gains a
  Check every column and `GET /api/status` gains
  `check_interval_s` per endpoint — done in 0.27.0.
- [x] Retry once before a failure counts (`retry_on_failure`):
  when a check fails it is immediately retried once, and only a
  second consecutive failure counts as DOWN for flap, alert, and
  uptime purposes; a blip that recovers on the retry is recorded
  as UP with the retry noted. On by default, disabled globally or
  per endpoint in the config or with `--no-retry-on-failure`; the
  retry is part of the same check round, so one history entry is
  recorded and the endpoint's next-due time is not reset. The
  dashboard gains a Retries column and `GET /api/status` gains
  `retries` / `retries_total` per endpoint; `--check` applies the
  same rule and prints "(retried once)" when a retry happened —
  done in 0.28.0.
- [x] SLA report CSV/JSON export (`--sla-format csv` / `json`,
  with `--sla-out PATH` to write it to a file): the same monthly
  report as CSV or JSON, adding per-endpoint incident counts,
  downtime inside the month, and latency p50/p95/p99 over the
  month — incident grouping and the uptime exclusions follow
  the same rules as the text report and the status page — done
  in 0.29.0.
- [x] SLA report over HTTP (`GET /api/sla` on the dashboard
  server): the same monthly SLA report as JSON — the month,
  one object per endpoint (uptime, checks, incidents,
  downtime, latency p50/p95/p99, target, met, shortfall), and
  the breach list — served live from the persisted history, so
  a script can poll it instead of re-running the export. With
  no query it serves the current month; `?month=YYYY-MM`
  names another month, and a bad month is a 400 with a JSON
  error. It reuses the export's payload builder, so the
  endpoint and `--sla-format json` can never disagree — done
  in 0.30.0.
- [x] Webhook alerting on SLA breach: while the monitor runs,
  each check round re-evaluates the current month's SLA report,
  and the first time an endpoint falls below its `sla_target`
  one webhook POST goes out with the breach details — the month,
  the month's uptime and checks, the target and the shortfall,
  incidents, downtime, and latency p50/p95/p99 — to the
  endpoint's own `webhook` when it has one, else the global
  `webhook_url`, reusing the state-change alert routing. One
  alert per endpoint per month (a new month starts fresh, and a
  SIGHUP reload does not re-fire one already sent); a failed
  delivery is logged and never stops the monitor. The breach
  and when it was alerted show on the dashboard (SLA breach
  column) and in `GET /api/status` (`sla_target`,
  `sla_breach`, `sla_shortfall_pct`, `sla_breach_alerted_at`),
  computed from the same report rows, so the live status and
  the report can never disagree. `--sla-alert [YYYY-MM]` fires
  the same alerts once from the command line and exits, for a
  cron job or a manual check — done in 0.31.0.
- [x] Down since / outage duration: the dashboard gains a Down
  since column showing how long the current outage has lasted
  ("12m 34s", `-` when not down), counted from the first failed
  check in the current run of failures — unconfirmed failures
  below the flap threshold count, so the clock starts when it
  first failed, not when the outage was confirmed; a success,
  skip, or maintenance round ends the run. `GET /api/status`
  gains `down_since` per endpoint (epoch seconds, or `null`
  when not down) and `GET /metrics` gains a
  `healthcheck_down_seconds` gauge (0 when up, omitted when
  never checked, skipped, or in maintenance), all computed from
  the same `History.down_since()` — done in 0.32.0.
- [x] Recent incidents in the JSON API: `GET /api/status` gains
  `recent_incidents` per endpoint (at most 5, newest first —
  each `{"start", "end", "checks", "ongoing", "duration_s"}`),
  so scripts consuming the API can see outage history without
  scraping the dashboard or regenerating the static status page.
  The grouping moved onto `History.recent_incidents()`, so the
  API, the static status page, and the SLA export all report the
  same incidents; the status page's `recent_incidents()` is now
  a thin wrapper over it. An ongoing incident has `end` `null`
  and its duration runs to the snapshot time — done in 0.33.0.
- [x] Recent incidents on the live dashboard: `GET /` gains a
  "Recent incidents" section below the endpoint table — one
  subsection per endpoint with recorded outages, newest first,
  at most 5, each with its start and end (or "ongoing", with the
  duration running to now), the duration, and how many failed
  checks it holds — from the same `History.recent_incidents()`
  the JSON API and the static status page use, so the three
  surfaces can never disagree. Endpoints with no incidents get
  no subsection, and with no incidents anywhere the section is
  omitted, so a quiet monitor's dashboard is unchanged — done
  in 0.34.0.
- [x] Recent incidents over HTTP (`GET /api/incidents` on the
  dashboard server): the same recent incidents per endpoint as
  JSON — newest first, at most 5 per endpoint, each `{"start",
  "end", "checks", "ongoing", "duration_s"}`, an ongoing
  incident's duration running to the request time — so a script
  can poll outage history without pulling the whole status
  snapshot. `?endpoint=NAME` filters to one endpoint and
  `?limit=N` caps the incidents per endpoint; an unknown
  endpoint or a bad limit is a 400 with a JSON error. It reuses
  `History.recent_incidents()`, so the endpoint, the dashboard,
  `GET /api/status`, and the static status page can never
  disagree — done in 0.35.0.
- [x] Incidents total in the Prometheus export: `GET /metrics`
  gains a `healthcheck_incidents_total` counter per endpoint —
  the total recorded incidents (runs of consecutive
  confirmed-down checks) from the same grouping the incident
  lists use, so the counter and the lists can never disagree;
  always present, even at 0 — done in 0.36.0.
- [x] `--incidents` CLI export: `--incidents` exports the
  recorded incident history — every recorded outage per
  endpoint, newest first, with per-endpoint incident counts and
  total downtime, and totals across endpoints — as plain text
  (default), CSV (one row per incident, `--incidents-format
  csv`), or JSON (`--incidents-format json`), with
  `--incidents-out PATH` to write it to a file. An ongoing
  incident has `end` `null` and its duration running to the
  export time. Built from the same `History.recent_incidents()`
  the dashboard, `GET /api/status`, `GET /api/incidents`, and
  the static status page use, so the export can never disagree
  with them — done in 0.37.0.
- [x] `--incidents` filters: `--endpoint NAME` exports only
  that endpoint's incidents and `--limit N` caps the incidents
  per endpoint, newest first — the same filters `GET
  /api/incidents` takes as `?endpoint=` and `?limit=`, reusing
  the same `History.recent_incidents()` helpers, so the CLI, the
  API, the dashboard, and the static page cannot disagree. An
  unknown endpoint or a non-positive limit is an error (exit 2),
  not an empty report; the totals always sum the rows actually
  exported — done in 0.38.0.
- [x] TLS certificate-expiry warnings: every HTTPS check reads
  the peer certificate's expiry (one extra short TLS handshake,
  since urllib does not expose the socket it used) and records
  it with the check, so the monitor can warn before the cert
  lapses. An expiring cert is a warning, not downtime — the
  endpoint stays UP. The dashboard gains a Cert expires column
  (whole days left, "(soon)" inside the warning window,
  "expired" once it has lapsed, "-" when no certificate was ever
  seen), `GET /api/status` gains `cert_expires_at` /
  `cert_expiry_days` / `cert_expiry_warn_days` /
  `cert_expiry_warning` per endpoint, and `GET /metrics` gains a
  `healthcheck_cert_expiry_days` gauge. An endpoint can set
  `cert_expiry_warn_days` (default 30, validated at load, `tcp`
  endpoints rejected); a check that cannot read the peer
  certificate reports no expiry instead of failing — done in
  0.39.0.
- [x] Webhook alerting on TLS certificate expiry: when an
  endpoint's certificate enters its warning window (its
  `cert_expiry_warn_days`, 30 by default), one webhook POST goes
  out with the expiry details (`type: "cert_expiry"`, the
  endpoint, `cert_expires_at`, `days_remaining`, `warn_days`,
  `checked_at`) — to the endpoint's own `webhook` when it has
  one, else the global `webhook_url`, reusing the state-change
  and SLA breach alert routing. One alert per certificate: the
  first check inside the window fires, later checks while the
  same certificate is still inside the window do not re-fire,
  and a renewed certificate fires again when it enters the
  window; a failed delivery is logged and the alert is still
  recorded as fired, so it is not re-sent. The alert never
  changes the endpoint's status — the endpoint stays UP — and a
  SIGHUP reload does not re-fire one already sent. The dashboard
  gains the alert time in the Cert expires column ("5d (soon)
  alerted 06:12") and `GET /api/status` gains
  `cert_expiry_alerted_at` per endpoint — done in 0.40.0.
- [x] Per-incident duration histogram in the `--incidents`
  text report: each endpoint's incidents are bucketed by
  duration into log-spaced buckets (`< 1m`, `1m – 5m`,
  `5m – 15m`, `15m – 1h`, `1h – 6h`, `6h – 24h`, `> 24h`),
  printed after the endpoint's incident lines (omitted when
  it has no incidents), from the same
  `History.recent_incidents()` / `_group_incidents()` data as
  the rest of the report — and the same `--endpoint` /
  `--limit` filters — so the histogram can never disagree
  with the incidents listed. The JSON export carries the same
  histogram per endpoint (`duration_histogram`) — done in
  0.41.0.
- [x] Duration histogram on the live dashboard: each endpoint's
  "Recent incidents" subsection shows the same per-endpoint
  duration histogram the `--incidents` report prints, from
  `History.incident_duration_histogram()` with the same limit
  of 5 as the incidents listed, so the dashboard, the text
  report, the JSON export and the API can never disagree —
  done in 0.42.0.
- [x] Duration histogram on the static status page: each
  endpoint's "Recent incidents" section shows the same
  per-endpoint duration histogram the `--incidents` report
  prints and the dashboard shows, from
  `History.incident_duration_histogram()` with the same limit
  of 5 as the incidents listed, so the static page, the
  dashboard, the text report, and the JSON export can never
  disagree — done in 0.43.0.
- [x] Duration histogram in the JSON API: `GET /api/status`
  gains `duration_histogram` per endpoint — the same 7
  log-spaced buckets the `--incidents` text report prints,
  from `History.incident_duration_histogram()` with the same
  limit of 5 as the `recent_incidents` listed, so the API, the
  dashboard, the static status page, the text report, and the
  JSON export can never disagree. All 7 buckets are always
  present, even at 0, so an endpoint with no incidents gets
  all zeroes instead of an omitted field — done in 0.44.0.
- [x] Duration histogram in the incidents API: `GET /api/incidents`
  gains `duration_histogram` per endpoint — the same 7
  log-spaced buckets the `--incidents` text report prints,
  from `History.incident_duration_histogram()` with the same
  `limit` as the `incidents` listed, so the API, the dashboard,
  the static status page, `GET /api/status`, the text report,
  and the JSON export can never disagree. All 7 buckets are
  always present, even at 0, so an endpoint with no incidents
  gets all zeroes instead of an omitted field — done in 0.45.0.
- [x] Uptime / SLA summary view: `GET /api/uptime` returns per
  endpoint the uptime % and total downtime over the last 24 hours,
  7 days, and 30 days (from `History.window_summary`, built on the
  same incident grouping the incident lists use — an outage that
  started before a window only counts the part inside it, and an
  ongoing outage accrues to the request time), with the endpoint's
  `sla_target` compared against the 30-day figure (`sla_met_30d`,
  `sla_shortfall_30d_pct`); `GET /api/status` gains
  `uptime_30d_pct` and `downtime_24h_seconds` /
  `downtime_7d_seconds` / `downtime_30d_seconds` per endpoint, and
  a remote status-page source reports its 30-day figure too —
  done in 0.46.0.
- [x] Uptime / SLA summary as a text export: `--uptime-report`
  prints the same per-endpoint summary `GET /api/uptime` returns
  — uptime %, total downtime, and counted checks over the last
  24 hours, 7 days, and 30 days, plus the `sla_target` verdict
  against the 30-day figure — as plain text, rendered from the
  same `uptime_snapshot` the API serves, so the text export and
  the API can never disagree. A window with no counted checks
  prints "no data" instead of a percentage, while its downtime
  is still shown; `--uptime-out PATH` writes the report to a
  file instead of printing it — done in 0.47.0.
- [x] 30-day uptime column on the dashboard and the static status
  page: the live dashboard gains a Uptime 30d column next to
  Uptime 24h / 7d, and the static status page (`--status-page`)
  gains one next to its Uptime 24h / 7d columns, so a slow
  degradation over the last month is visible at a glance next to
  the shorter windows. Both read the same figures `GET
  /api/uptime` and `--uptime-report` serve — the dashboard from
  `uptime_snapshot`, the static page from the same
  `uptime_pct_window` the API reports as `uptime_30d_pct` (a
  remote status-page source reports its own 30-day figure the
  same way, since it carries no per-check records for
  `window_summary` to work from) — so the two surfaces can never
  disagree with the API or the text export. A window with no
  counted checks shows `-`, like the 24h/7d columns — done in
  0.48.0.
- [x] 30-day uptime ratio gauge in `GET /metrics`:
  `healthcheck_uptime_ratio_30d` joins the all-time, 24h, and 7d
  uptime-ratio gauges, so a slow degradation over the last month
  is visible to Prometheus scrapers next to the shorter windows.
  It reads the same `uptime_pct_window` figure `GET /api/uptime`
  reports as `uptime_30d_pct`, so the gauge and the API can never
  disagree, and a check 10 days old shows up in the 30d gauge
  while the 24h/7d gauges omit it. A window with no counted
  checks has no series, like the 24h/7d gauges — done in 0.49.0.
- [x] Per-endpoint HTTP method (`method`, e.g. `HEAD` for a
  headers-only check or `POST` for a health endpoint that only
  answers to POST): the check sends that method with no body
  (GET by default), and the status, body assertions, flap, and
  alert rules judge the answer exactly like a GET. Only `GET`,
  `HEAD`, `POST`, `PUT`, `DELETE`, `PATCH`, and `OPTIONS` are
  accepted, case-insensitively and normalized to upper case; a
  bad method, a non-string value, or the key on a `tcp`
  endpoint is rejected at config load, and a HEAD check cannot
  be combined with `expect_body` / `expect_body_regex` since a
  HEAD response has no body. The dashboard gains a Method column
  and `GET /api/status` gains `method` per endpoint — done in
  0.50.0.
- [x] Per-endpoint custom request headers (`headers`, e.g.
  `{"Authorization": "Bearer ..."}` for a health endpoint behind
  auth, or `{"X-Api-Key": "..."}`): the check sends those headers
  with every request, verbatim, alongside the configured method;
  endpoints without `headers` behave exactly as before. A
  non-object value, a non-string header name or value, or the key
  on a `tcp` endpoint is rejected at config load. The values are
  secrets and are never shown: the dashboard gains a Headers
  column and `GET /api/status` gains `headers` per endpoint, both
  listing only the header names (sorted, `null` / `-` when the
  endpoint sends none) — done in 0.51.0.
- [x] Per-endpoint consecutive-failures counter: the current run
  of failed checks that actually ran, from the persisted history
  (unconfirmed failures below the flap threshold count, a success
  or a skipped / maintenance round ends the run, `0` when up or
  never checked) — shown as a Fail streak column on the dashboard
  and the static status page, and as `consecutive_failures` in
  `GET /api/status`, next to `down_since`, which starts from the
  same run, so the count and the outage clock can never disagree.
  An operator reads it against the configured `flap_threshold`
  (3 by default) to see how close a still-UP endpoint is to being
  confirmed down — done in 0.52.0.
- [x] Consecutive-failures gauge in `GET /metrics`:
  `healthcheck_consecutive_failures` exposes the same
  `consecutive_failures` streak per endpoint, so a Prometheus
  server can alert on it (e.g. when it reaches the configured
  `flap_threshold`) — always present, even at 0, and reading the
  same `History.consecutive_failures()` figure `GET /api/status`
  reports, so the gauge and the API can never disagree — done in
  0.53.0.
- [x] Per-endpoint alert threshold (`alert_threshold`): an
  endpoint can declare how many consecutive failures must pile
  up before its outage pages, independent of the flap threshold
  that flips it DOWN — a noisy endpoint can flip DOWN at 3
  failures but wait until 5 before it pages, and one that
  recovers before its threshold never pages at all (and no
  recovery alert follows, since it never paged). An endpoint
  without one alerts at confirmation, like before; a value at
  or below the flap threshold alerts at confirmation too. Must
  be a positive integer, validated at load and on SIGHUP reload;
  `--alert-threshold NAME=N` (repeatable, flag wins) sets it per
  endpoint. The dashboard gains an Alert at column and `GET
  /api/status` gains `alert_threshold` per endpoint, next to the
  Fail streak / `consecutive_failures` they are read against —
  done in 0.54.0.
- [x] Per-endpoint alert cooldown / re-alert interval
  (`alert_cooldown_s`): an endpoint that stays down pages again
  once that many seconds have passed since its last down alert —
  a long outage re-pages instead of going quiet after the first
  page, and the repeat page carries `"repeat": true` in its
  webhook payload. The first page of an outage is immediate, a
  recovery clears the cooldown state (so the next outage pages
  immediately and the recovery alert is never delayed), and an
  endpoint without one alerts exactly once per outage, like
  before. It can also be set globally in the config as the
  default for endpoints without their own. Must be a positive
  number of seconds, validated at load and on SIGHUP reload;
  `--alert-cooldown NAME=SECONDS` (repeatable, flag wins) sets
  it per endpoint. The dashboard gains a Cooldown column (with
  the time until the next page while the endpoint is DOWN) and
  `GET /api/status` gains `alert_cooldown_s`,
  `last_alerted_at`, and `alert_cooldown_remaining_s` per
  endpoint — done in 0.55.0.
- [x] Alert at / Cooldown columns on the static status page: the
  `--status-page` output gains the same Alert at and Cooldown
  columns the dashboard shows, right after the Fail streak — the
  endpoint's own `alert_threshold` (the flap threshold in effect
  when it sets none, resolved by the CLI before the page is
  written) and its `alert_cooldown_s` (`-` when it never
  re-pages). A static snapshot carries no alert records, so the
  page shows the configured cooldown, not the live re-alert
  countdown the dashboard appends while an endpoint is DOWN —
  done in 0.56.0.
- [x] Down since column on the static status page: the
  `--status-page` output gains the same Down since column the
  dashboard shows, right after Status and before the Fail
  streak — how long the current outage has lasted ("12m 34s",
  `-` when not down), counted from the first failed check in
  the current run of failures (unconfirmed failures included)
  to the page's generated time, from the same
  `History.down_since()` the dashboard reads, so the two can
  never disagree. A still-UP endpoint with failures below the
  flap threshold shows `-`, like the dashboard — its run is
  visible in the Fail streak instead — done in 0.57.0.
- [x] Cert expires, Method, Headers, and HTTP status columns on
  the static status page: the `--status-page` output gains the
  same four columns the dashboard shows, right after the Cooldown
  column and in the same order — the method the endpoint is
  checked with (`-` for the default GET and for TCP endpoints),
  the names of the extra request headers it sends (never the
  values, `-` when it sends none), the expected vs actual HTTP
  status (`-` when no status was ever returned), and whole days
  until the TLS certificate expires (`89d`, `12d (soon)` inside
  the warning window, `expired` once it has lapsed, `-` when no
  certificate was ever seen). The four computations moved into
  shared helpers the dashboard reads too, so the two pages can
  never disagree; a snapshot carries no alert records, so the
  page does not append the time a cert-expiry alert fired, and
  a remote source shows `-` for all four — done in 0.58.0.
- [x] Latency p50 / p95 / p99 column on the static status page: the
  `--status-page` output gains the same column the dashboard
  shows, right after Latency and before the latency sparkline —
  nearest-rank p50/p95/p99 over the last 24 hours of recorded
  latencies, from a shared `latency_percentiles_text` helper the
  dashboard reads too, so the two pages can never disagree. `-`
  when the endpoint has no recorded latencies in the window
  (never checked, only skips / maintenance), and for a remote
  source, whose snapshot carries no per-check records — done in
  0.59.0.
- [x] Latency budget, Check every, and Retries columns on the
  static status page: the `--status-page` output gains the same
  three columns the dashboard shows, right after Latency and
  before the latency percentiles, in the same order — the
  endpoint's `max_latency_ms` (`-` when it has none), its
  `check_interval_s` (`-` when it uses the global interval), and
  the retries spent on the most recent recorded check (`-` when
  never checked). The three computations moved into shared
  helpers the dashboard reads too, so the two pages can never
  disagree; a remote source shows `-` for all three, since its
  snapshot carries no endpoint config and no per-check records —
  done in 0.60.0.
- [x] Last error column on the static status page: the
  `--status-page` output gains the same column the dashboard
  shows, right after the latency sparkline and before Last check
  — the error string of the most recent failed check that
  actually ran (`-` when the endpoint never failed), from a
  shared `endpoint_last_error_text` helper the dashboard reads
  too, so the two pages can never disagree. It stays after a
  later success, so a recovered endpoint still shows why it last
  failed; a remote source shows `-`, since its snapshot carries
  no per-check records — done in 0.61.0.
- [x] SLA breach column on the static status page: the
  `--status-page` output gains the same column the dashboard
  shows, right after Last error and before Last check —
  `BREACH -X.Xpp` when the endpoint is below its `sla_target`
  in the page's calendar month (`-` when it has no target, no
  data in the month, or meets the target), from a shared
  `endpoint_sla_breach_text` helper the dashboard reads too, so
  the two pages can never disagree. A snapshot carries no alert
  records, so no "alerted" time is appended, unlike the live
  dashboard; an extra source's endpoints carry no config, so they
  have no target and always show `-` — done in 0.62.0.
- [x] Depends on / Maintenance columns on the static status page:
  the `--status-page` output gains the same two columns the
  dashboard shows, right after Cert expires and before Uptime —
  the name from `depends_on` (`-` when the endpoint has no
  dependency) and the endpoint's declared maintenance windows
  (`Sat 02:00–04:00`, several joined with `; `, `-` when it
  declares none), from shared `endpoint_depends_on_text` and
  `endpoint_maintenance_text` helpers the dashboard reads too, so
  the two pages can never disagree; an extra source's endpoints
  carry no config, so they show `-` for both — done in 0.63.0.
- [x] Downtime column on the static status page: the
  `--status-page` output gains a Downtime 30d column right after
  Uptime 30d — total outage time inside the last 30 days (`1h
  23m`, `-` when the endpoint recorded no downtime in the window),
  from a shared `endpoint_downtime_text` helper in server.py built
  on the same `History.downtime_seconds_window` the API reports
  as `downtime_30d_seconds`, so the page can never disagree with
  it. An outage that started before the window only counts the
  part inside it, and an ongoing outage accrues to the page's
  generated time; a remote source shows `-`, since its snapshot
  carries no per-check records, while a file source shows its
  recorded downtime, like a local one — done in 0.64.0.
- [x] Downtime column on the live dashboard: the dashboard gains
  the same Downtime 30d column the static status page shows,
  right after Uptime 30d — total outage time inside the last 30
  days (`1h 23m`, `-` when the endpoint recorded no downtime in
  the window), from the same shared `endpoint_downtime_text`
  helper in server.py the page reads, so the two can never
  disagree. An outage that started before the window only counts
  the part inside it, and an ongoing outage accrues to the page's
  render time — done in 0.65.0.
- [x] Downtime gauges in `GET /metrics`:
  `healthcheck_downtime_seconds_24h` / `_7d` / `_30d` expose the
  total outage time in seconds inside the last 24 hours / 7 days /
  30 days per endpoint, so a Prometheus server can alert on
  cumulative downtime (e.g. when the 30-day downtime passes a
  budget). Each reads the same `History.downtime_seconds_window`
  figure `GET /api/status` reports as `downtime_24h_seconds` /
  `downtime_7d_seconds` / `downtime_30d_seconds`, so the gauges
  and the API can never disagree — an outage that started before
  a window only counts the part inside it, and an ongoing outage
  accrues to the request time. Always present, even at 0, like
  `healthcheck_incidents_total` — done in 0.66.0.
- [x] Downtime report as a text export: `--downtime-report`
  prints the per-endpoint downtime over the last 24 hours / 7
  days / 30 days on its own, plus a total across endpoints per
  window, rendered from the same `uptime_snapshot` the API
  serves and `--uptime-report` prints, so the export can never
  disagree with either — an outage that started before a window
  only counts the part inside it, an ongoing outage accrues to
  the report time, and "0s" when an endpoint recorded no
  downtime in a window. `--downtime-out PATH` writes it to a
  file instead — done in 0.67.0.
- [x] Per-endpoint p95 latency alerts: an endpoint can declare a
  `latency_p95_threshold_ms`, and when the windowed p95 over the
  last 24 hours crosses it the endpoint is in a latency-alert
  state — advisory only, it stays UP. Set in the config file or
  with `--latency-p95-threshold NAME=MS` (repeatable, flag wins),
  validated at load and on SIGHUP reload; the dashboard gains a
  Latency p95 alert column and `GET /api/status` gains
  `latency_p95_threshold_ms` / `latency_p95_alert` per endpoint,
  both from the same shared `endpoint_latency_p95_alert` helper,
  so they can never disagree — done in 0.68.0. One webhook alert
  per crossing (a sustained breach pages exactly once, and a p95
  that falls back below the threshold re-arms) arrived in
  0.69.0, with `latency_p95_alerted_at` in `GET /api/status`
  and the alert time appended in the dashboard column.

## Changelog

- **0.69.0** — Webhook alert when the p95 crosses its threshold:
  `LatencyP95Alerter` fires one POST per crossing — a
  `latency_p95_breach` payload (endpoint, p95, threshold, the
  24-hour window, the most recent check time) to the endpoint's
  own `webhook` URL when it has one, else the global
  `webhook_url` — from the same shared
  `endpoint_latency_p95_alert` helper the dashboard and `GET
  /api/status` read, so the firing decision can never disagree
  with what they show. A sustained breach pages exactly once,
  and a p95 that falls back below its threshold re-arms, so the
  next crossing fires again; a failed delivery is logged and
  still recorded as alerted, so it is not re-sent; an endpoint
  with no webhook anywhere sends nothing and does not consume
  its crossing. The alert time shows as
  `latency_p95_alerted_at` in `GET /api/status` and appended to
  the dashboard's Latency p95 alert column ("ALERT 812.3 > 400
  ms alerted 12:30"), and a SIGHUP reload does not re-fire an
  alert sent for an ongoing breach. Next: a Latency p50 / p95 /
  p99 column on the static status page.

- **0.68.0** — Per-endpoint p95 latency alerts:
  `latency_p95_threshold_ms` declares the p95 latency an
  endpoint must stay within; when the windowed p95 over the last
  24 hours crosses it, the endpoint is in a latency-alert state —
  advisory only, it stays UP and no webhook fires — shown as
  `ALERT 812.3 > 400 ms` in the dashboard's new Latency p95
  alert column and as `latency_p95_alert: true` in `GET
  /api/status`, both from the same shared
  `endpoint_latency_p95_alert` helper, so they can never
  disagree. Set in the config file or with
  `--latency-p95-threshold NAME=MS` (repeatable, flag wins),
  validated at load and on SIGHUP reload; exactly at the
  threshold is not a breach, and no data in the window is not a
  slowdown. Next: a webhook alert when the p95 crosses its
  threshold (one alert per crossing, like the SLA breach
  alerter).

- **0.67.0** — Downtime report as a text export:
  `--downtime-report` prints the per-endpoint downtime over the
  last 24 hours / 7 days / 30 days on its own, plus a total
  across endpoints per window, rendered from the same
  `uptime_snapshot` the API serves and `--uptime-report` prints,
  so the export can never disagree with either — an outage that
  started before a window only counts the part inside it, an
  ongoing outage accrues to the report time, and "0s" when an
  endpoint recorded no downtime in a window. `--downtime-out
  PATH` writes it to a file instead. Next: per-endpoint latency
  alerts when p95 crosses a threshold.

- **0.66.0** — Downtime gauges in `GET /metrics`:
  `healthcheck_downtime_seconds_24h` / `_7d` / `_30d` expose the
  total outage time in seconds inside the last 24 hours / 7 days /
  30 days per endpoint, so a Prometheus server can alert on
  cumulative downtime (e.g. when the 30-day downtime passes a
  budget). Each reads the same `History.downtime_seconds_window`
  figure `GET /api/status` reports as `downtime_24h_seconds` /
  `downtime_7d_seconds` / `downtime_30d_seconds`, so the gauges
  and the API can never disagree — an outage that started before
  a window only counts the part inside it, and an ongoing outage
  accrues to the request time. Always present, even at 0, like
  `healthcheck_incidents_total`.

- **0.65.0** — Downtime column on the live dashboard: the dashboard
  gains the same Downtime 30d column the static status page shows,
  right after Uptime 30d — total outage time inside the last 30
  days (`1h 23m`, `-` when the endpoint recorded no downtime in
  the window), from the same shared `endpoint_downtime_text`
  helper in server.py the page reads, so the two can never
  disagree. An outage that started before the window only counts
  the part inside it, and an ongoing outage accrues to the page's
  render time.

- **0.64.0** — Downtime column on the static status page: the
  `--status-page` output gains a Downtime 30d column right after
  Uptime 30d — total outage time inside the last 30 days (`1h
  23m`, `-` when the endpoint recorded no downtime in the window),
  from a shared `endpoint_downtime_text` helper in server.py built
  on the same `History.downtime_seconds_window` the API reports
  as `downtime_30d_seconds`, so the page can never disagree with
  it. An outage that started before the window only counts the
  part inside it, and an ongoing outage accrues to the page's
  generated time; a remote source shows `-`, since its snapshot
  carries no per-check records, while a file source shows its
  recorded downtime, like a local one.

- **0.63.0** — Depends on / Maintenance columns on the static
  status page: the `--status-page` output gains the same two
  columns the dashboard has, right after Cert expires and before
  Uptime — the name from `depends_on` (`-` when the endpoint has
  no dependency) and the endpoint's declared maintenance windows
  (`Sat 02:00–04:00`, several joined with `; `, `-` when it
  declares none), from shared `endpoint_depends_on_text` and
  `endpoint_maintenance_text` helpers the dashboard reads too, so
  the two pages can never disagree; an extra source's endpoints
  carry no config, so they show `-` for both.

- **0.62.0** — SLA breach column on the static status page: the
  `--status-page` output gains the same column the dashboard has,
  right after Last error and before Last check — `BREACH -X.Xpp`
  when the endpoint is below its `sla_target` in the page's
  calendar month (`-` when it has no target, no data in the month,
  or meets the target), from a shared
  `endpoint_sla_breach_text` helper the dashboard reads too, so
  the two pages can never disagree. A snapshot carries no alert
  records, so no "alerted" time is appended, unlike the live
  dashboard; an extra source's endpoints carry no config, so they
  have no target and always show `-`.

- **0.61.0** — Last error column on the static status page: the
  `--status-page` output gains the same column the dashboard has,
  right after the latency sparkline and before Last check — the
  error string of the most recent failed check that actually ran
  (`-` when the endpoint never failed), from a shared
  `endpoint_last_error_text` helper the dashboard reads too, so
  the two pages can never disagree. It stays after a later
  success, so a recovered endpoint still shows why it last
  failed; a remote source shows `-`, since its snapshot carries
  no per-check records.

- **0.60.0** — Latency budget, Check every, and Retries columns on
  the static status page: the `--status-page` output gains the
  same three columns the dashboard has, right after Latency and
  before the latency percentiles, in the same order — the
  endpoint's `max_latency_ms` (`-` when it has none), its
  `check_interval_s` (`-` when it uses the global interval), and
  the retries spent on the most recent recorded check (`-` when
  never checked). The three computations moved into shared helpers
  the dashboard reads too, so the two pages can never disagree; a
  remote source shows `-` for all three, since its snapshot
  carries no endpoint config and no per-check records.

- **0.59.0** — Latency p50 / p95 / p99 column on the static status
  page: the `--status-page` output gains the same column the
  dashboard has, right after Latency and before the latency
  sparkline — nearest-rank p50/p95/p99 over the last 24 hours of
  recorded latencies, from a shared `latency_percentiles_text`
  helper the dashboard reads too, so the two pages can never
  disagree. `-` when the endpoint has no recorded latencies in
  the window (never checked, only skips / maintenance), and for a
  remote source, whose snapshot carries no per-check records.

- **0.58.0** — Cert expires, Method, Headers, and HTTP status
  columns on the static status page: the `--status-page` output
  gains the same four columns the dashboard has, right after the
  Cooldown column and in the same order — the method the endpoint
  is checked with (`-` for the default GET and for TCP endpoints),
  the names of the extra request headers it sends (never the
  values, `-` when it sends none), the expected vs actual HTTP
  status (`expected 204, got 200` when the endpoint asserts on a
  code, the bare actual code when it does not, `-` when no status
  was ever returned), and whole days until the TLS certificate
  expires (`89d`, `12d (soon)` inside the warning window,
  `expired` once it has lapsed, `-` when no certificate was ever
  seen). The four computations moved into shared helpers the
  dashboard reads too, so the two surfaces can never disagree. A
  snapshot carries no alert records, so the page does not append
  the time a cert-expiry alert fired, and a remote source shows
  `-` for all four columns.
  `--status-page` output gains the same Down since column the
  dashboard has, right after Status and before the Fail streak —
  how long the current outage has lasted ("12m 34s", `-` when
  not down), counted from the first failed check in the current
  run of failures (unconfirmed failures included) to the page's
  generated time, from the same `History.down_since()` the
  dashboard reads, so the two surfaces can never disagree. A
  still-UP endpoint with failures below the flap threshold shows
  `-`, like the dashboard — its run is visible in the Fail streak
  instead.

- **0.56.0** — Alert at / Cooldown columns on the static status
  page: the `--status-page` output gains the same Alert at and
  Cooldown columns the dashboard has, right after the Fail
  streak — the endpoint's own `alert_threshold` (the flap
  threshold in effect when it sets none, resolved by the CLI
  before the page is written) and its `alert_cooldown_s`
  (`-` when it never re-pages). A static snapshot carries no
  alert records, so it shows the configured cooldown rather
  than the live dashboard's time until the next re-page while
  an endpoint is DOWN — the same value the dashboard shows
  once the endpoint has recovered.
- **0.55.0** — Per-endpoint alert cooldown / re-alert interval: an
  endpoint that stays down pages again once `alert_cooldown_s`
  seconds have passed since its last down alert — a long outage
  re-pages instead of going quiet after the first page, and the
  repeat page carries `"repeat": true` in its webhook payload.
  The first page of an outage is immediate, a recovery clears the
  cooldown state (so the next outage pages immediately and the
  recovery alert is never delayed), and an endpoint without one
  alerts exactly once per outage, like before. The cooldown can
  also be set globally in the config as the default for endpoints
  without their own; it must be a positive number of seconds,
  validated at config load and on SIGHUP reload, and
  `--alert-cooldown NAME=SECONDS` (repeatable, flag wins) sets or
  replaces it per endpoint. The dashboard gains a Cooldown column
  (with the time until the next page while the endpoint is DOWN)
  and `GET /api/status` gains `alert_cooldown_s`,
  `last_alerted_at`, and `alert_cooldown_remaining_s` per endpoint.
- **0.54.0** — Per-endpoint alert threshold: an endpoint can set
  `alert_threshold` (e.g. 5) in the config, and its outage only
  pages once that many consecutive failures have piled up — the
  flap threshold still flips it DOWN (at 3 by default), but the
  webhook / digest alert waits for the alert threshold, so a
  noisy endpoint pages later than it flips and one that recovers
  before its threshold never pages at all (and no recovery alert
  follows, since the alerter never saw it go down). An endpoint
  without one defaults to the flap threshold in effect and alerts
  at confirmation, like before; a value at or below the flap
  threshold alerts at confirmation too. Validated at config load
  (a positive integer) and on SIGHUP reload; `--alert-threshold
  NAME=N` (repeatable, flag wins) sets or replaces it per
  endpoint. The dashboard gains an Alert at column and `GET
  /api/status` gains `alert_threshold` per endpoint, next to the
  Fail streak / `consecutive_failures` they are read against —
  done in 0.54.0.
- **0.53.0** — Consecutive-failures gauge in `GET /metrics`:
  `healthcheck_consecutive_failures` exposes the current run of
  failed checks per endpoint, from the same
  `History.consecutive_failures()` figure `GET /api/status`
  reports as `consecutive_failures`, so the gauge and the API can
  never disagree. Always present, even at 0 (a never-checked
  endpoint is 0, a success resets the run to 0), so the series is
  safe to alert on — an operator alerts when it reaches the
  configured `flap_threshold` (3 by default).
- **0.52.0** — Per-endpoint consecutive-failures counter: the
  current run of failed checks that actually ran, from the
  persisted history — unconfirmed failures below the flap
  threshold count, a success or a skipped / maintenance round ends
  the run, `0` when the endpoint is up or was never checked. The
  dashboard and the static status page gain a Fail streak column
  and `GET /api/status` gains `consecutive_failures` per endpoint,
  next to `down_since`, which starts from the same run, so the
  count and the outage clock can never disagree. An operator reads
  it against the configured `flap_threshold` (3 by default) to see
  how close a still-UP endpoint is to being confirmed down.
- **0.51.0** — Per-endpoint custom request headers: an `http`
  endpoint can set `headers` (e.g. `{"Authorization": "Bearer
  ..."}` for a health endpoint behind auth, or `{"X-Api-Key":
  "..."}`) and the check sends those headers with every request,
  verbatim, alongside the configured HTTP method. A non-object
  value, a non-string header name or value, or the key on a `tcp`
  endpoint is rejected at config load, and endpoints without
  `headers` behave exactly as before. The values are secrets and
  are never shown: the dashboard gains a Headers column and `GET
  /api/status` gains `headers` per endpoint, both listing only the
  header names (sorted, `null` / `-` when the endpoint sends
  none).
- **0.50.0** — Per-endpoint HTTP method: an `http` endpoint can
  set `method` (e.g. `HEAD` or `POST`) and the check sends that
  method with no body instead of always GETting — a HEAD check
  asks for headers only, a POST check suits a health endpoint
  that only answers to POST. The status, body assertions, flap
  detection, and alerts judge the answer exactly like a GET. Only
  `GET`, `HEAD`, `POST`, `PUT`, `DELETE`, `PATCH`, and `OPTIONS`
  are accepted (case-insensitive, normalized to upper case); a
  bad method, a non-string value, or the key on a `tcp` endpoint
  is rejected at config load, and a HEAD check cannot be combined
  with `expect_body` / `expect_body_regex` since a HEAD response
  has no body. The dashboard gains a Method column and `GET
  /api/status` gains `method` per endpoint (`null` when the
  endpoint uses the default GET).
- **0.49.0** — 30-day uptime ratio gauge in `GET /metrics`:
  `healthcheck_uptime_ratio_30d` joins the all-time, 24h, and 7d
  uptime-ratio gauges, reading the same `uptime_pct_window`
  figure `GET /api/uptime` reports as `uptime_30d_pct`, so the
  gauge and the API can never disagree. A window with no counted
  checks has no series, like the 24h/7d gauges.
- **0.48.0** — 30-day uptime column on the dashboard and the
  static status page: the live dashboard gains a Uptime 30d
  column next to Uptime 24h / 7d, and the static status page
  gains one next to its Uptime 24h / 7d columns. Both read the
  same figures `GET /api/uptime` and `--uptime-report` serve —
  the dashboard from `uptime_snapshot` (the same helper the API
  and the text report use), the static page from the same
  `uptime_pct_window` the API reports as `uptime_30d_pct` — so
  the surfaces can never disagree with each other, the API, or
  the text export. A remote status-page source shows its own
  reported 30-day figure, since a snapshot carries no per-check
  records. A window with no counted checks shows `-`, like the
  24h/7d columns. (Also fixes the version constant, which the
  0.47.0 run left at 0.46.0.)
- **0.47.0** — Uptime / SLA summary as a text export: a new
  `--uptime-report` flag prints the same per-endpoint summary
  `GET /api/uptime` returns — uptime %, total downtime, and
  counted checks over the last 24 hours, 7 days, and 30 days,
  plus the endpoint's `sla_target` compared against the 30-day
  figure (`met`, `short by Xpp`, `no 30d data`, or `none`) — as
  plain text, rendered from the same `uptime_snapshot` the API
  serves, so the text export and the API can never disagree.
  An outage that started before a window only counts the part
  inside it, an ongoing outage accrues downtime to the report
  time, and a window with no counted checks reports "no data"
  with the downtime still counted. `--uptime-out PATH` writes
  the report to a file instead of printing it.
- **0.46.0** — Uptime / SLA summary view: a new `GET /api/uptime`
  endpoint returns per endpoint the uptime % and total downtime
  over the last 24 hours, 7 days, and 30 days, plus the
  endpoint's `sla_target` compared against the 30-day figure
  (`sla_met_30d`, `sla_shortfall_30d_pct`). The figures come from
  `History.window_summary`, built on the same incident grouping
  the incident lists use, so an outage that started before a
  window only counts the part inside it, an ongoing outage
  accrues downtime to the request time, and an endpoint with no
  counted checks in a window reports uptime `null` with the
  downtime still counted. `GET /api/status` gains
  `uptime_30d_pct` and `downtime_24h_seconds` /
  `downtime_7d_seconds` / `downtime_30d_seconds` per endpoint
  (its window figures now also honour the snapshot's `now`,
  like the latency percentiles already did), and a remote
  status-page source reports its 30-day figure too.
- **0.45.0** — Per-incident duration histogram in the incidents
  API: `GET /api/incidents` gains `duration_histogram` per
  endpoint — the same 7 log-spaced buckets (`< 1m`, `1m – 5m`,
  `5m – 15m`, `15m – 1h`, `1h – 6h`, `6h – 24h`, `> 24h`) the
  `--incidents` text report prints, from
  `History.incident_duration_histogram()` with the same `limit`
  as the `incidents` listed alongside it (so a `?limit=1`
  response's histogram covers only that one incident), so the
  histogram can never disagree with the incidents above it.
  An ongoing incident's duration runs to the request time in
  both, and all 7 buckets are always present even at 0 — an
  endpoint with no incidents gets all zeroes, not an omitted
  field.
- **0.44.0** — Per-incident duration histogram in the JSON
  API: `GET /api/status` gains `duration_histogram` per
  endpoint — the same 7 log-spaced buckets (`< 1m`, `1m – 5m`,
  `5m – 15m`, `15m – 1h`, `1h – 6h`, `6h – 24h`, `> 24h`) the
  `--incidents` text report prints, from
  `History.incident_duration_histogram()` with the same limit
  of 5 as the `recent_incidents` listed alongside it, so the
  histogram can never disagree with the incidents above it.
  An ongoing incident's duration runs to the snapshot time in
  both, and all 7 buckets are always present even at 0 — an
  endpoint with no incidents gets all zeroes, not an omitted
  field.
- **0.43.0** — Per-incident duration histogram on the static
  status page's "Recent incidents" section: each endpoint's
  section shows its incidents bucketed by duration into the
  same 7 log-spaced buckets the `--incidents` text report
  prints, from `History.incident_duration_histogram()` with
  the same limit of 5 as the incidents listed, so the static
  page, the dashboard, the text report, and the JSON export can
  never disagree. The incident list and the histogram share the
  page's generated time as their reference, so an ongoing
  incident's duration is the same in both. An endpoint with no
  incidents gets no histogram, like on the dashboard.
- **0.42.0** — Per-incident duration histogram on the live
  dashboard's "Recent incidents" section: each endpoint's
  subsection shows its incidents bucketed by duration into the
  same 7 log-spaced buckets the `--incidents` text report
  prints, from `History.incident_duration_histogram()` with
  the same limit of 5 as the incidents listed, so the
  histogram can never disagree with the incidents above it.
- **0.41.0** — Per-incident duration histogram in the
  `--incidents` text report: `History.incident_duration_histogram()`
  buckets an endpoint's incidents by duration into log-spaced
  buckets (`< 1m`, `1m – 5m`, `5m – 15m`, `15m – 1h`, `1h – 6h`,
  `6h – 24h`, `> 24h`), from the same `_group_incidents()`
  grouping the incident lists use, with the same `--endpoint` /
  `--limit` filters applied, so the histogram can never
  disagree with the incidents the export lists. The text report
  prints the histogram after each endpoint's incident lines
  (omitted for an endpoint with no incidents), and the JSON
  export carries it per endpoint as `duration_histogram`; the
  CSV is unchanged (one row per incident). An ongoing
  incident's duration runs to the export time, like everywhere
  else.

- **0.40.0** — Webhook alerting on TLS certificate expiry: when
  an endpoint's certificate enters its warning window (its
  `cert_expiry_warn_days`, 30 by default), one webhook POST goes
  out with the expiry details — `{"type": "cert_expiry",
  "endpoint", "cert_expires_at", "days_remaining", "warn_days",
  "checked_at"}` — to the endpoint's own `webhook` when it has
  one, else the global `webhook_url`, reusing the state-change
  and SLA breach alert routing (`CertExpiryAlerter`, evaluated
  after every check round in the monitor loop). One alert per
  certificate: the first check inside the window fires, later
  checks while the same certificate is still inside the window
  do not re-fire, and a renewed certificate (a different expiry)
  fires again when it enters the window; a failed delivery is
  logged and the alert is still recorded as fired, like the SLA
  breach alerter. A SIGHUP reload does not re-fire an alert
  already sent for the same certificate. The alert never changes
  the endpoint's status — the endpoint stays UP — and the
  dashboard shows when it fired in the Cert expires column
  ("5d (soon) alerted 06:12") while `GET /api/status` gains
  `cert_expiry_alerted_at` per endpoint. Tested end to end
  against a local TLS server with an openssl-generated
  2-day certificate: exactly one webhook POST across two
  monitor rounds, endpoint still UP.

- **0.39.0** — TLS certificate-expiry warnings: every HTTPS check
  reads the peer certificate's `notAfter` (one extra short TLS
  handshake against the same host, verified with the same context
  — the custom CA and client cert when the endpoint sets them)
  and records it as `cert_expires_at` on the check result, so
  the dashboard, `GET /api/status`, and `GET /metrics` can warn
  before the cert lapses. The dashboard gains a Cert expires
  column ("89d", "12d (soon)" inside the endpoint's warning
  window, "expired" once it has lapsed, "-" when no certificate
  was ever seen); `GET /api/status` gains `cert_expires_at`,
  `cert_expiry_days`, `cert_expiry_warn_days`, and
  `cert_expiry_warning` per endpoint; `GET /metrics` gains a
  `healthcheck_cert_expiry_days` gauge (omitted when no expiry
  was recorded). An endpoint can set `cert_expiry_warn_days`
  (default 30, a positive number, validated at config load and
  rejected on `tcp` endpoints). An expiring cert is a warning,
  not downtime — the endpoint stays UP — and a check that cannot
  read the peer certificate reports no expiry instead of failing.

- **0.38.0** — `--incidents` filters: `--endpoint NAME` exports
  only that endpoint's incidents and `--limit N` caps the
  incidents per endpoint, newest first — mirroring the `GET
  /api/incidents` query params and reusing the same
  `History.recent_incidents()` helpers the export already uses,
  so the CLI, the API, the dashboard, and the static page cannot
  disagree. An unknown endpoint is an error (exit 2), like the
  API's 400 for a bad `?endpoint=`; a non-positive or
  non-integer limit is also exit 2. `--endpoint` and `--limit`
  without `--incidents` are rejected, like the other
  `--incidents-*` flags. The totals always sum the rows actually
  exported.

- **0.37.0** — `--incidents` CLI export: `--incidents` exports
  the recorded incident history — every recorded outage per
  endpoint, newest first, with per-endpoint incident counts and
  total downtime, and totals across endpoints — as plain text
  (default), CSV (one row per incident), or JSON, with
  `--incidents-out PATH` to write it to a file. An ongoing
  incident has `end` `null` and its duration running to the
  export time. Built from the same `History.recent_incidents()`
  the dashboard, `GET /api/status`, `GET /api/incidents`, and
  the static status page use, so the export can never disagree
  with them; `--incidents-format` and `--incidents-out` without
  `--incidents` are rejected, like the SLA flags.

- **0.36.0** — Incidents total in the Prometheus export: `GET
  /metrics` gains a `healthcheck_incidents_total` counter per
  endpoint — the total recorded incidents (runs of consecutive
  confirmed-down checks), from the same `History.incidents_total()`
  grouping the incident lists use, so the counter and the lists
  can never disagree. Always present, even at 0; the grouping
  moved onto a shared `History._group_incidents()`, so the counter,
  `GET /api/incidents`, the dashboard, `GET /api/status`, and the
  static status page all report the same incidents.

- **0.35.0** — Recent incidents over HTTP: `GET /api/incidents`
  serves the recent incidents per endpoint as JSON — newest
  first, at most 5 per endpoint, each `{"start", "end", "checks",
  "ongoing", "duration_s"}`, an ongoing incident's duration
  running to the request time — so a script can poll outage
  history without pulling the whole status snapshot.
  `?endpoint=NAME` filters to one endpoint and `?limit=N` caps
  the incidents per endpoint; an unknown endpoint or a bad limit
  is a 400 with a JSON error. It reuses
  `History.recent_incidents()`, so the endpoint, the dashboard,
  `GET /api/status`, and the static status page can never
  disagree.

- **0.34.0** — Recent incidents on the live dashboard: `GET /`
  gains a "Recent incidents" section below the endpoint table —
  one subsection per endpoint with recorded outages, newest
  first, at most 5, each with its start and end (or "ongoing",
  with the duration running to now), the duration, and how many
  failed checks it holds — from the same
  `History.recent_incidents()` the JSON API and the static status
  page use, so the three surfaces can never disagree. Endpoints
  with no incidents get no subsection, and with no incidents
  anywhere the section is omitted, so a quiet monitor's
  dashboard is unchanged.

- **0.33.0** — Recent incidents in the JSON API: `GET /api/status`
  gains `recent_incidents` per endpoint (at most 5, newest first —
  each `{"start", "end", "checks", "ongoing", "duration_s"}`), so
  scripts consuming the API can see outage history without scraping
  the dashboard or regenerating the static status page. The incident
  grouping moved onto `History.recent_incidents()`, so the API, the
  static status page, and the SLA export all report the same
  incidents; the status page's `recent_incidents()` is now a thin
  wrapper over it. An ongoing incident has `end` `null` and its
  duration runs to the snapshot time.

- **0.32.0** — Down since / outage duration: the dashboard gains
  a Down since column showing how long the current outage has
  lasted ("45s", "12m 34s", "2h 5m", "3d 4h" — the two largest
  units), counted from the first failed check in the current run
  of failures; unconfirmed failures below the flap threshold
  count, so the clock starts when it first failed, not when the
  outage was confirmed, and a success, skip, or maintenance
  round ends the run. `GET /api/status` gains `down_since` per
  endpoint (epoch seconds, `null` when not down) and `GET
  /metrics` gains a `healthcheck_down_seconds` gauge (0 when up,
  omitted for never-checked, skipped, or maintenance endpoints),
  all from the same `History.down_since()` computation, so the
  three surfaces can never disagree.

- **0.31.0** — Webhook alerting on SLA breach: while the
  monitor runs, each check round re-evaluates the current
  month's SLA report, and the first time an endpoint falls
  below its `sla_target` one webhook POST goes out with the
  breach details (`type: "sla_breach"`, the endpoint, month,
  uptime_pct, checks, incidents, downtime_seconds, target,
  shortfall_pct, latency p50/p95/p99) — to the endpoint's own
  `webhook` when it has one, else the global `webhook_url`.
  One alert per endpoint per month; a new month starts fresh,
  a SIGHUP reload does not re-fire one already sent, and a
  failed delivery is logged and never stops the monitor. The
  dashboard gains an SLA breach column and `GET /api/status`
  gains `sla_target`, `sla_breach`, `sla_shortfall_pct`, and
  `sla_breach_alerted_at` per endpoint, all computed from the
  same report rows as `--sla-report`. `--sla-alert [YYYY-MM]`
  fires the same alerts once from the command line and exits.

- **0.30.0** — SLA report over HTTP: `GET /api/sla` on the
  dashboard server returns the monthly SLA report as JSON — the
  month, one object per endpoint (uptime_pct, checks,
  failed_checks, incidents, downtime_seconds, latency p50/p95/
  p99, sla_target, met, shortfall_pct), and the breach list.
  With no query it serves the current month (local time);
  `?month=YYYY-MM` names another month, and a bad month is a
  400 with a JSON `{"error": ...}` body. The endpoint and the
  `--sla-format json` export share one payload builder
  (`sla_report_payload`), so the two can never disagree; the
  rows come from the same `sla_report` computation, with the
  same exclusion rules (skips, unconfirmed failures, and
  maintenance rounds count neither for nor against the month).

- **0.29.0** — SLA report CSV/JSON export: `--sla-report
  YYYY-MM --sla-format csv` / `json` exports the monthly SLA
  report instead of printing it as text, and `--sla-out PATH`
  writes it to a file. The export adds per-endpoint incident
  counts, downtime inside the month, and latency p50/p95/p99
  over the month, reusing the text report's uptime math and the
  status page's incident grouping (consecutive confirmed-down
  checks are one incident; skips, unconfirmed failures, and
  maintenance rounds neither start nor end one; downtime is
  clipped to the month, and an incident still open at the
  month's end accrues downtime to the month end). The text
  report is unchanged and stays the default; `--sla-format`
  and `--sla-out` without `--sla-report` are rejected.

- **0.28.0** — Retry once before a failure counts: when a check
  fails it is immediately retried once, on the same endpoint with
  the same check, and only a second consecutive failure counts as
  DOWN for flap, alert, and uptime purposes. A first-attempt
  failure that recovers on the retry is recorded as UP, with
  `retries: 1` on the check — the retry is part of the same check
  round, not a second check, so one history entry is recorded and
  the endpoint's next-due time is not reset by it. Retries are on
  by default; set `retry_on_failure` to `false` globally or per
  endpoint to disable them, or pass `--no-retry-on-failure` for
  the run. A bad value is rejected at config load, and a SIGHUP
  reload picks up changes. The dashboard gains a Retries column
  and `GET /api/status` gains `retries` / `retries_total` per
  endpoint; `--check` applies the same rule and prints "(retried
  once)" on the line when a retry happened.

- **0.27.0** — Per-endpoint check intervals: an endpoint can set
  `check_interval_s` (e.g. 60) in the config, and it is checked
  on its own cadence instead of the global `interval_seconds`
  (which stays the default for endpoints without one). The
  scheduler tracks when each endpoint is next due and sleeps
  until the next one, so a fast endpoint does not wait on a slow
  one and a slow one is not checked every round. A dependent that
  is due while its dependency is not due this round is judged
  against the dependency's last recorded check, so a down
  dependency still skips it. `--check-interval NAME=SECONDS`
  (repeatable) sets or replaces it for one endpoint and wins over
  the config file; a bad value is rejected at config load, and a
  SIGHUP reload picks up changes (a bad edit keeps the running
  config). The dashboard gains a Check every column and `GET
  /api/status` gains `check_interval_s` per endpoint (`null`
  when the endpoint uses the global interval).

- **0.26.0** — Last error per endpoint: the dashboard gains a
  Last error column showing the error string of the endpoint's
  most recent failed check (HTML-escaped, `-` when it never
  failed), and `GET /api/status` gains `last_error` per endpoint.
  Only checks that actually ran count — skipped and maintenance
  rounds are excluded — and the error stays after a later success,
  so a recovered endpoint still shows why it last failed.

- **0.25.0** — Dry-run mode: `--check` validates the config, runs
  one check pass over every endpoint, prints the per-endpoint
  verdict (UP/DOWN with status and latency, the rule that failed
  the check named in the error, SKIPPED with the dependency named,
  MAINTENANCE with the window), and exits 0 when all checked
  endpoints are up or in maintenance, 1 when any is down.
  `--check NAME` checks just that endpoint (with its dependencies,
  so a dependent can be judged at all); an unknown name is
  rejected with exit code 2. Nothing is persisted, no alert fires,
  and no dashboard starts, so it is safe to run from a deploy
  script or CI. A single failed check is DOWN — the flap threshold
  needs repeated rounds and does not apply to a one-shot pass.

- **0.24.0** — Per-endpoint latency budgets: an endpoint can set
  `max_latency_ms` (e.g. 500) in the config, and a check that
  answers slower is DOWN, with the actual latency and the budget
  named in the error (`latency 812.3 ms exceeds budget 500 ms`),
  and the failure goes through the same flap detection, alert,
  and uptime rules as any other failed check — a single slow
  check is unconfirmed, and the endpoint only flips to DOWN
  after the threshold's consecutive slow checks. The slow latency
  is still recorded, so the dashboard and `GET /api/status` show
  how slow it was next to the budget. `--max-latency NAME=MS`
  (repeatable) sets or replaces it for one endpoint and wins over
  the config file; a bad value is rejected at config load, and a
  SIGHUP reload picks up changes (a bad edit keeps the running
  config). The dashboard gains a Latency budget column and `GET
  /api/status` gains `max_latency_ms` per endpoint. Works for
  `http` and `tcp` endpoints alike.

- **0.23.0** — Per-endpoint expected HTTP status: an `http`
  endpoint can set `expect_status` (e.g. 204) in the config, and
  only that exact status counts as up — any other status is DOWN,
  with the expected and actual codes named in the error, and the
  failure goes through the same flap detection, alert, and uptime
  rules as any other failed check. An expected 4xx/5xx status
  counts too (urllib raises for those, so the error path checks
  the expectation before failing). Body assertions still apply
  once the status matches. `--expect-status NAME=STATUS`
  (repeatable) sets or replaces it for one endpoint and wins over
  the config file; a bad value, a non-integer, or the key on a
  `tcp` endpoint is rejected at config load, and a SIGHUP reload
  picks up changes (a bad edit keeps the running config). The
  dashboard gains an HTTP status column (`expected 204, got 200`,
  the bare code, or `-`) and `GET /api/status` gains
  `expect_status` and `status_code` per endpoint.

- **0.22.0** — Status-page sources in the config file: a
  `status_page_sources` list of `{"label", "path"}` and
  `{"label", "url"}` entries, so file and remote sources survive
  without repeating `--status-page-source` every time. Entries
  are validated at config load and on SIGHUP reload — a bad entry
  (no label, both or neither of `path`/`url`, a non-http(s) url,
  a duplicate label) logs a warning and is skipped, never taking
  the monitor down, and a reload with a bad entry still applies
  the rest of the new config. `--status-page-source` flags merge
  with the config list: a flag with the same label replaces that
  entry, a new label is added. The dashboard gains a "Status page
  sources" section and `GET /api/status` gains a
  `status_page_sources` key listing the merged sources, both kept
  in sync across a SIGHUP reload.

- **0.21.0** — Remote status-page source: `--status-page-source
  LABEL=URL` scrapes a remote healthcheck instance's `GET
  /api/status` over HTTP and renders it as a labelled section of
  the static status page, with the same status, uptime 24h/7d and
  banner rules per source — no local history file needed for the
  second target. `ApiStatusHistory` wraps the snapshot so the
  existing page renderer judges it like a local source; a snapshot
  carries no per-check records, so a remote source shows no
  latency sparkline and no incident list (none is invented from a
  single sample). An unreachable instance, a non-200 response,
  bad JSON, or a payload that is not a status snapshot logs a
  warning and renders the section empty instead of failing the
  page; a URL already ending in `/api/status` is used as-is.

- **0.20.0** — Second scrape target for the static status page:
  `--status-page-source LABEL=PATH` (repeatable) merges another
  healthcheck instance's persisted history file into the same
  `--status-page` output, under its own labelled section (the local
  history is labelled `local`), with its endpoints attributed to
  that source. Status, uptime 24h/7d and incident grouping are
  computed per source with the same rules as the local one, so an
  endpoint with the same name in both sources is judged separately
  in each. A missing or corrupt second history logs a warning and
  renders empty instead of taking the page down; a spec without
  `LABEL=` is rejected, and `--status-page-source` without
  `--status-page` is rejected. With no extra source the page is
  byte-identical to before.

- **0.19.0** — Per-endpoint alert routing: an endpoint can set its
  own `webhook` URL in the config, so its state-change alerts go to
  its own channel instead of the global `webhook_url`, and its
  slice of the alert digest — its changes in the window, and its
  currently-down entry — is POSTed to its own webhook as a
  separate digest. Endpoints without one fall back to the global
  URL (or the digest block's `webhook_url`); the digest email
  still covers every endpoint. A bad per-endpoint URL (not an
  http(s) URL) is rejected at config load. Also fixed: `make_alerter`
  used to pre-validate the `alert_digest` block and hand the
  normalized dict to `DigestAlerter`, which re-validated it and
  rejected its own `smtp` key — any config with a digest block
  crashed the monitor at startup; the raw block is now passed
  through and validated once.

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
