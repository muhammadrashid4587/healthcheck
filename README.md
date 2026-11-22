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
- A plain HTML dashboard: endpoint, status, uptime %, latency, last check.

## What it is not

- Not a replacement for a real observability platform (no metrics
  pipeline, no alerting integrations, no distributed tracing).
- Alerts are just log lines. No email, no SMS, no webhooks, no PagerDuty.
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
- `http` endpoint — `url` required; ok on status 200–399.
- `tcp` endpoint — `host` and `port` required; ok when the connect
  succeeds.

## Dashboard

`GET /` renders a table with one row per endpoint: name, current status
(UP / DOWN / UNKNOWN), uptime % over the recorded history, most recent
latency, and last check time. Any other path returns 404.

## Tests

```sh
make test   # python3 -m unittest discover -s tests -v
```

## Roadmap

- Configurable alert hooks (webhook URL) — log lines only for now.
- Per-endpoint latency history sparkline on the dashboard.
- HTTPS endpoints with custom CA / client certificates.

## Changelog

- **0.2.0** — Persist history to disk across restarts: `history_file`
  in the config (or `--history-file`), loaded on startup, saved after
  every check round and on shutdown. Corrupt history files log a
  warning and start empty instead of crashing.
