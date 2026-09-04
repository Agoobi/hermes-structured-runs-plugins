# Hermes Structured Runs Plugin

`structured-runs` is a Hermes Agent user plugin that wraps the existing `/v1/runs` API and adds a JSON Schema finalizer plus authenticated media routes.

It does **not** patch or replace Hermes core. It forwards work to the real Hermes API server, so the run still uses the normal Hermes agent loop and tools.

## Module layout

The plugin is a directory package; Hermes loads `__init__.py` with
`submodule_search_locations` set, so the modules below use normal `from . import`
relative imports.

| Module | Responsibility |
|---|---|
| `__init__.py` | `register(ctx)`: load state, build the app, start the server thread. |
| `_config.py` | Env-derived settings and shared constants (one source of truth). |
| `_state.py` | In-memory run registry + JSON persistence: load, save, crash recovery, retention/cap eviction. |
| `_session_db.py` | Read-only access to Hermes `state.db` + the session-settle wait. |
| `_schema.py` | Optional `jsonschema` validation of the finalizer contract. |
| `_media.py` | Artifact path resolution (traversal-safe) + media-URL enrichment. |
| `_upstream.py` | Allowlisted HTTP client for the real API server. |
| `_events.py` | Per-run SSE event buffer + fan-out; the drainer also runs the finalizer on terminal. |
| `_finalize.py` | Post-completion agent check + `complete_structured` finalizer. |
| `_app.py` | The aiohttp routes. |

## What it adds

- `POST /v1/runs/structured` — create a normal Hermes run, plus `json_schema` for the final response.
- `GET /v1/runs/structured/{run_id}` — poll the upstream run and return `parsed` JSON once complete.
- `GET /v1/runs/structured/{run_id}/events` — SSE stream backed by a **per-run event buffer** (see below). Supports reconnect with replay via `Last-Event-ID` or `?after=<seq>`. Emits a final `structured.completed` / `structured.failed` / `structured.skipped` event when terminal.
- `GET /v1/runs/structured/{run_id}/events/log` — the buffered events as a plain JSON array (a normal fetch, not a stream), plus the current structured result. `?after=<seq>` returns only newer events.
- `POST /v1/runs/structured/{run_id}/stop` — pass through to upstream run stop.
- `POST /v1/runs/structured/{run_id}/approval` — pass through approval responses.
- The finalizer first launches a **post-completion agent check in the same session** (by default only when finalizing the agent's own output is not schema-valid — see `STRUCTURED_RUNS_FINAL_CHECK_MODE`).
- `GET /v1/runs/structured/{run_id}/media?path=...` — serve attached image/video/audio artifacts safely.

## API reference (OpenAPI + Swagger UI)

`docs/openapi.json` is a hand-maintained OpenAPI 3.1 spec covering the plugin's routes (`:8646`) plus the Hermes core routes it wraps / you call directly (`:8642`).

```bash
bun run swagger          # validate the spec, then serve Swagger UI on http://localhost:8677
bun run swagger -- 9000  # pick a port
bun run swagger:lint     # validate only (no server) — usable in CI
```

`preswagger` runs `bun install` first; Swagger UI assets are served from the `swagger-ui-dist` package (works offline after the first install).

## Install

Copy the plugin directory into your Hermes user plugins folder:

```bash
mkdir -p ~/.hermes/plugins
cp -R structured-runs ~/.hermes/plugins/structured-runs
hermes plugins enable structured-runs
hermes gateway restart
```

Verify:

```bash
curl http://localhost:8646/health
```

Expected:

```json
{
  "status": "ok",
  "plugin": "structured-runs",
  "upstream": "http://127.0.0.1:8642",
  "jsonschema_validation": true
}
```

## Usage

Create a structured run:

```bash
curl -X POST http://localhost:8646/v1/runs/structured \
  -H "Authorization: Bearer $API_SERVER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "input": "Use the terminal tool to run date and return the result.",
    "json_schema": {
      "type": "object",
      "properties": {
        "used_tool": {"type": "boolean"},
        "command": {"type": "string"},
        "current_datetime": {"type": "string"},
        "summary": {"type": "string"}
      },
      "required": ["used_tool", "command", "current_datetime", "summary"],
      "additionalProperties": false
    }
  }'
```

Poll:

```bash
curl -H "Authorization: Bearer $API_SERVER_KEY" \
  http://localhost:8646/v1/runs/structured/{run_id}
```

SSE:

```bash
curl -N -H "Authorization: Bearer $API_SERVER_KEY" \
  http://localhost:8646/v1/runs/structured/{run_id}/events
```

Example completed response:

```json
{
  "object": "hermes.run",
  "run_id": "run_xxx",
  "status": "completed",
  "output": "raw Hermes output",
  "structured": true,
  "structured_status": "completed",
  "parsed": {
    "used_tool": true,
    "command": "date",
    "current_datetime": "Sat Aug 8 12:24:16 UTC 2026",
    "summary": "..."
  },
  "content_type": "json",
  "structured_model": "gpt-5.5",
  "structured_validation": "enforced",
  "structured_error": null
}
```

## Post-completion output check

Every successful structured run first waits for its own background delegations to complete and be delivered, then takes the newest persisted assistant reply from the same session. This avoids freezing an early final answer while QA or other `delegate_task` work is still returning.

**Attempt 0** — finalize **the agent's own output** into schema-valid JSON (`llm.complete_structured`, no agent turn).

- If it is schema-valid (and `jsonschema` is installed), the run completes there. No agent turn is spent.
- If not, the wrapper runs **re-check agent turns in the same session**: the agent reviews the client JSON Schema and returns a corrected final answer, which is finalized again. This loops up to `STRUCTURED_RUNS_FINAL_CHECK_MAX_ATTEMPTS` (default 3, **hard cap 7**), feeding the last validation error into each retry prompt. The first attempt that produces valid JSON is committed.

`STRUCTURED_RUNS_FINAL_CHECK_MODE`: `auto` (default, re-check only when needed) · `always` (at least one re-check every run) · `off` (never re-check).

`STRUCTURED_RUNS_FINAL_CHECK_STOP_ON_FALLBACK` (default 2): stop the loop early after this many consecutive re-checks whose agent turn could not run.

Every attempt is recorded in `final_output_check`:

```json
{
  "status": "skipped" | "completed" | "exhausted" | "fallback",
  "reason": "first_pass_schema_valid" | "final_check_disabled" | null,
  "recheck_attempts": 2,
  "resolved_on_attempt": 2,
  "last_recheck_run_id": "run_zzz",
  "history": [
    { "attempt": 0, "kind": "agent_output",  "outcome": "invalid", "validation_error": "...", "finalizer_text_preview": "..." },
    { "attempt": 1, "kind": "agent_recheck", "recheck_run_id": "run_yyy", "recheck_status": "completed", "outcome": "invalid", "validation_error": "..." },
    { "attempt": 2, "kind": "agent_recheck", "recheck_run_id": "run_zzz", "recheck_status": "completed", "outcome": "valid" }
  ]
}
```

| `status` | meaning |
|---|---|
| `skipped` | no re-check (attempt 0 valid in `auto`, or `mode=off`) |
| `completed` | a re-check produced valid JSON (`resolved_on_attempt`) |
| `exhausted` | ran all `MAX_ATTEMPTS`, still not valid → `structured_status: "failed"` |
| `fallback` | stopped early because agent turns kept failing to run |

**Cost/latency**: worst case is `MAX_ATTEMPTS` agent turns (each ~30–60s + tools + tokens) and `MAX_ATTEMPTS + 1` finalizer calls; `structured_status` stays `running` for the whole loop. Each re-check also adds a user/assistant message pair to the session. Keep `MAX_ATTEMPTS` low (2–3) unless a schema is genuinely hard.

The wrapper resolves any artifact path mentioned in the reply against `STRUCTURED_RUNS_MEDIA_ROOTS` **before** the finalizer runs. Verified paths are passed to the agent (when a re-check runs) as authoritative absolute paths. The finalizer canonicalizes existing `*_path` values to bare absolute paths; `MEDIA:` remains a delivery marker.

A re-check that cannot start, fails, or times out falls back to the original completed output — it never erases a valid result.

If the durable delegation state cannot be read (a locked `state.db` or a Hermes-core schema change), the settle step does **not** treat that as "nothing pending": it keeps polling until `STRUCTURED_RUNS_SESSION_SETTLE_TIMEOUT_S` and reports `session_settle.status = "timeout"`. A deployment with no `state.db` at all reports `"unavailable"` and skips the wait. A warning is logged when a query against `state.db` fails.

## Run events

Hermes core's `/v1/runs/{id}/events` is a single `asyncio.Queue` per run: a second subscriber steals events from the first, there is no replay, and the queue is dropped the moment any subscriber disconnects — even mid-run. Proxying it directly means a client that reconnects loses everything and only sees `status` polling afterwards.

Instead, from the moment a structured run is created the wrapper opens **one** upstream events subscription and drains it into a bounded per-run buffer:

- `GET .../events` replays the buffer (or from `Last-Event-ID` / `?after=<seq>`), then tails new events, with `: keepalive` comments every `STRUCTURED_RUNS_SSE_KEEPALIVE_S`.
- `GET .../events/log` returns the buffer as JSON (`events`, `next_after`, `closed`, `upstream_state`) plus the current `structured` result.
- when the run reaches a terminal state the drainer runs the finalizer once and appends the `structured.*` event — so the structured result is produced even if the only client used SSE and disconnected early, or never connected at all.
- if upstream has no record of the run and no session can be recovered, the drainer emits `structured.failed` (`structured_error: "run_not_found_upstream"`) after `STRUCTURED_RUNS_SSE_UNKNOWN_TIMEOUT_S` instead of polling forever.

Buffers hold up to `STRUCTURED_RUNS_EVENT_LOG_MAX_EVENTS` events, survive `STRUCTURED_RUNS_EVENT_LOG_TTL_S` after the run closes (for late replay), and are capped at `STRUCTURED_RUNS_EVENT_LOG_MAX_RUNS` runs. They are in-memory only — a gateway restart loses events from before the restart.

## Crash recovery

`_finalize_structured` marks a run `structured_status = "running"` before its settle / final-check / finalizer steps. If the gateway restarts during that window, the wrapper rewinds any such run (that has not reached `structured_done`) back to `pending` on load, so the next poll re-runs the finalizer.

## Media artifacts

If the structured result contains a local media path, such as:

```json
{
  "media_path": "videos/my-run/output/video.mp4",
  "video_url": null
}
```

and the file exists under an allowed media root, the plugin enriches the response:

```json
{
  "media_path": "videos/my-run/output/video.mp4",
  "video_url": "/v1/runs/structured/run_xxx/media?path=videos%2Fmy-run%2Foutput%2Fvideo.mp4"
}
```

Fetch it with auth:

```bash
curl -L -H "Authorization: Bearer $API_SERVER_KEY" \
  "http://localhost:8646/v1/runs/structured/run_xxx/media?path=videos%2Fmy-run%2Foutput%2Fvideo.mp4" \
  -o video.mp4
```

Browser note: a plain `<video src="...">` cannot attach an `Authorization` header. Use `fetch()` + `Blob` URL, or add a separate signed URL route if you need direct `<video src>` playback.

## Configuration

Environment variables:

| Variable | Default | Description |
|---|---|---|
| `STRUCTURED_RUNS_UPSTREAM` | `http://127.0.0.1:8642` | Upstream Hermes API server. |
| `STRUCTURED_RUNS_MAX_OUTPUT_CHARS` | `200000` | Max raw output passed into the finalizer. |
| `STRUCTURED_RUNS_FINAL_CHECK_MODE` | `auto` | When the re-check runs: `auto` = only when the finalizer output is not schema-valid; `always` = at least one re-check per run; `off` = never. |
| `STRUCTURED_RUNS_FINAL_CHECK_MAX_ATTEMPTS` | `3` | Max re-check agent turns (attempt 0 excluded). **Hard-clamped to `[0, 7]`.** |
| `STRUCTURED_RUNS_FINAL_CHECK_STOP_ON_FALLBACK` | `2` | Stop the loop after this many consecutive re-checks whose agent turn could not run (`0` disables). |
| `STRUCTURED_RUNS_FINAL_CHECK_TEXT_PREVIEW_CHARS` | `500` | Chars of raw finalizer text kept per `final_output_check.history` entry. |
| `STRUCTURED_RUNS_FINAL_CHECK_TIMEOUT_S` | `120` | Maximum time for one re-check agent turn. |
| `STRUCTURED_RUNS_FINAL_CHECK_POLL_INTERVAL_S` | `1` | Seconds between status checks for that correction turn. |
| `STRUCTURED_RUNS_SESSION_SETTLE_TIMEOUT_S` | `180` | Max wait for this session's background delegations/delivery before finalization. |
| `STRUCTURED_RUNS_SESSION_QUIET_S` | `3` | Required quiet window after delegated output delivery. |
| `STRUCTURED_RUNS_SESSION_SETTLE_POLL_INTERVAL_S` | `1` | Seconds between durable delegation-state checks. |
| `STRUCTURED_RUNS_SSE_UNKNOWN_TIMEOUT_S` | `90` | Grace period before the SSE poll-fallback emits `structured.failed` for a run that upstream has no record of and no recoverable session. |
| `STRUCTURED_RUNS_SSE_KEEPALIVE_S` | `15` | Idle interval between `: keepalive` comments on the `/events` stream. |
| `STRUCTURED_RUNS_EVENT_LOG_MAX_EVENTS` | `3000` | Max events buffered per run. |
| `STRUCTURED_RUNS_EVENT_LOG_TTL_S` | `600` | How long a closed run's event buffer is kept for late replay. |
| `STRUCTURED_RUNS_EVENT_LOG_MAX_RUNS` | `500` | Max runs with a live event buffer (oldest closed dropped first). |
| `STRUCTURED_RUNS_STATE_DB_BUSY_TIMEOUT_S` | `5` | SQLite busy timeout when reading Hermes `state.db` (it is written by Hermes core). |
| `STRUCTURED_RUNS_RETENTION_S` | `604800` (7 days) | Finished runs older than this are dropped from the registry / state file. `0` disables. |
| `STRUCTURED_RUNS_MAX_TRACKED` | `2000` | Hard cap on tracked runs; oldest finished runs are dropped first once exceeded. In-flight runs are never dropped. `0` disables. |
| `STRUCTURED_RUNS_MEDIA_ROOTS` | `/root/motion-graphic-templete,/root/.hermes,/tmp` | Comma-separated roots allowed for media serving. |

State is persisted at:

```text
~/.hermes/structured_runs_state.json
```

The state file stores schemas, structured results, and terminal upstream snapshots. It intentionally does **not** persist request `Authorization` headers.

The registry is bounded: on each save, finished runs older than `STRUCTURED_RUNS_RETENTION_S` are dropped, and if more than `STRUCTURED_RUNS_MAX_TRACKED` runs remain, the oldest finished ones are dropped until the cap is met. In-flight runs are never dropped. A dropped run polls like any run unknown to the wrapper (`structured_error: "schema_mapping_not_found"`).

## Security model

- All wrapper calls should use the same `Authorization: Bearer ...` header as the upstream Hermes API server.
- Cached structured results are **not** returned when upstream auth rejects the request (`401` / `403`).
- Media serving is locked down:
  - caller must pass upstream auth;
  - `run_id` must be known to the wrapper;
  - requested `path` must already be attached in the run's parsed JSON as a `*_path` field;
  - resolved file must be under `STRUCTURED_RUNS_MEDIA_ROOTS`;
  - `../` traversal is rejected (relative and via the `MEDIA:` marker), as are symlinks that resolve outside a root and paths containing a NUL byte;
  - sqlite databases (`*.db`, `*.sqlite`, `*.sqlite3` and their `-wal` / `-shm` / `-journal` sidecars) and this wrapper's own `structured_runs_state.json` are never served, even when they sit under an allowed root (the default roots include `~/.hermes`).

## Limitations

- This is a wrapper on port `8646`, not a core patch to Hermes `/v1/runs` on port `8642`.
- The final structured JSON is produced by an additional `ctx.llm.complete_structured(...)` call after the upstream run completes.
- JSON Schema validation (including `additionalProperties: false`) is only enforced when the `jsonschema` package is installed. The completed response reports `"structured_validation": "enforced"` or `"skipped_no_jsonschema"`, and `/health` reports `jsonschema_validation`. When it is skipped a warning is logged per run.
- If you need direct browser media tags without auth headers, add signed temporary media URLs.
