# Local load tests (monitoring, Phase M7)

Reproducible, **synthetic** load tests for DocPipe with and without the operator
monitoring. Results describe the test machine, not a production server.
The measured results and their interpretation are in
[docs/architecture/monitoring-dashboard.md §17](../docs/architecture/monitoring-dashboard.md).

## What runs where

Everything runs in Docker on one host, in its own network `dpm7-net`. All
containers use the image built from this repository:

| Container | Role | Limits |
|---|---|---|
| `dpm7-app` | DocPipe (`uvicorn ... --workers N --no-proxy-headers`) | `--cpus 2 --memory 1g` (`APP_CPUS`, `APP_MEM`) |
| `dpm7-fake` | [`fake_upstream.py`](fake_upstream.py): Ollama and Stirling stand-in over real HTTP, fixed synthetic delays (AI 150 ms ±20 %, PDF 40 ms ±20 %) | none |
| load generator | [`loadgen.py`](loadgen.py) in the network namespace of `dpm7-app`, so the dashboard is reached over loopback (development mode without TLS) | `--cpus 1.5` (`LOAD_CPUS`) |

**Configurations** ([`prepare.py`](prepare.py)):

- `off`: monitoring disabled.
- `collect`: data collection only.
- `web`: collection plus operator interface, with real Argon2id users and TOTP
  logins.

**Request mix:** `health`, `extract` (synthetic one-page PDF) and `analyze`
(maintenance mode, schema-valid answer from the fake). Latencies are measured
client-side; the warm-up time is not counted.

## Scenarios ([`run.sh`](run.sh))

| Scenario | Content |
|---|---|
| `overhead` | Per configuration, three interleaved repetitions: 60 s idle CPU, RSS, then 30 s sequential requests (concurrency 1), `health` and `extract` |
| `matrix` | 50 concurrent mixed requests for 60 s, 1 and 4 workers, all three configurations (`web` with 3 dashboard sessions) |
| `dash` | 4 workers, 50 concurrent API requests plus 8 dashboard sessions polling every 0.25 s |
| `probes` | Status endpoints of the fake trickle, hang or close the connection while 50 concurrent requests run |
| `unreachable` | The upstream container stops for 20 s during the load (4 workers) |
| `overflow` | `buffer_max_requests: 100`, flush every 30 s: drops must be counted, never silent |
| `pressure` | Database pre-filled to 24 MiB with `storage.max_bytes: 16 MiB`: pruning and storage limit under load |

## Running

```sh
loadtest/run.sh build            # docpipe-m7:local from the working tree
loadtest/run.sh overhead matrix dash probes unreachable overflow pressure
loadtest/run.sh cleanup          # removes dpm7-* containers and the network
```

Results: `$WORKROOT/results/<run>/` (default `/tmp/dpm7`). Each run contains:

- `setup.json` – configuration, workers and limits;
- `load.json` – throughput, mean, p50/p95/p99/max, status codes and the number
  of requests sent;
- `db.json` – rows, data-quality sums, events, file sizes;
- `proc_*.json` – RSS and CPU ticks per process;
- `idle.json` – idle CPU in % of one core;
- `docker_stats.json`.

Never run this against a real installation: the scripts create their own
configuration, synthetic users and keys, and delete their working directory.
