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
  endpoint.
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
  logged, not paged. The threshold is configurable per config.
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
  so it can go into a log or a script.
- Down since: when an endpoint is DOWN the dashboard shows how long
  the outage has lasted ("12m 34s"), from the first failed check in
  the current run of failures — unconfirmed failures below the flap
  threshold count, so the clock starts when it first failed, not
  when the outage was confirmed. The same start time is in
  `GET /api/status` (`down_since`, epoch seconds, `null` when not
  down) and in `GET /metrics` as a `healthcheck_down_seconds` gauge
  (0 when up).
- A static status page: `python3 -m healthcheck.cli --config config.json --status-page status.html`
  renders the same view as the dashboard — current status, uptime
  24h/7d/30d, latency sparkline — plus a recent-incidents list per
  endpoint, as one self-contained HTML file with no external
  references, so it can be hosted anywhere with no server. It is a
  snapshot of the persisted history; regenerate it to update it.
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
  sparkline and no incident list for a remote source, and an
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
  webhook, flap threshold, and status
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
                  # intervals, webhook, flap threshold, and
                  # status page sources apply before the next
                  # check round
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
current run of failures, unconfirmed ones included), the HTTP status
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
in that window — no data is not downtime), most recent
latency, the endpoint's latency budget (`max_latency_ms`, or `-`
when it has none), the endpoint's own check interval
(`check_interval_s`, or `-` when it uses the global interval),
a latency p50 / p95 / p99 column (nearest-rank percentiles
over the last 24 hours, `-` when no checks ran in that window), a sparkline
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
return, or `null` when it accepts any 2xx/3xx), `status_code` (the
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
`null` when the endpoint is not currently down), `recent_incidents`
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
checks that were up, all-time) plus `healthcheck_uptime_ratio_24h`
and `healthcheck_uptime_ratio_7d`, `healthcheck_cert_expiry_days`
(gauge, whole days until the endpoint's TLS certificate expires —
negative when expired, omitted when no expiry was recorded),
`healthcheck_latency_ms` (summary
with `quantile="0.5" / "0.95" / "0.99"` in ms over the last 24 hours,
plus `_sum` and `_count`), `healthcheck_checks_total` (counter,
split by `result="success"` / `"failure"`), and
`healthcheck_incidents_total` (counter, total recorded incidents
per endpoint — runs of consecutive confirmed-down checks — from
the same grouping the incident lists use, so the counter and the
lists can never disagree; always present, even at 0). The same
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

## Changelog

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
