"""Read-only operator monitoring (Phase M2: foundation, no UI, no routes).

Design and threat model: docs/architecture/monitoring-dashboard.md.

Everything in this package is inert unless `monitoring.enabled: true` is set
in the server config: no database, no files, no threads, no upstream
requests, no routes. The only always-installed piece is the ASGI middleware
in system/main.py, which passes every request straight through while
`app.state.monitoring` is None.

Two invariants hold for every module here:

- Monitoring never breaks or slows a DocPipe request on purpose: every hook
  swallows its own errors (and counts them), buffers are bounded, and
  nothing blocks on the database inside a request.
- Only allow-listed, structured values are stored: enums from
  system/monitoring/registry.py, ids from the server config, numbers and
  UTC timestamps. Never bodies, file names, prompts, answers, headers or
  error message texts.
"""
