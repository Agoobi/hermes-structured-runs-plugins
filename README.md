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
| `_finalize.py` | Post-completion agent check + `complete_structured` finalizer. |
| `_app.py` | The aiohttp routes. |

## What it adds

- `POST /v1/runs/structured` — create a normal Hermes run, plus `json_schema` for the final response.
- `GET /v1/runs/structured/{run_id}` — poll the upstream run and return `parsed` JSON once complete. The poll never blocks longer than `STRUCTURED_RUNS_POLL_FINALIZE_WAIT_S` on an in-flight finalizer; it answers `structured_status: "running"` and the next poll collects the result.
- `GET /v1/runs/structured/{run_id}/events` — proxy upstream SSE events, fall back to polling if upstream events are unavailable, and emit a final `structured.completed` / `structured.failed` / `structured.skipped` event only when terminal. A run whose structured result is already terminal gets that event immediately, even if upstream has since dropped the run record. If upstream never has a record of the run and no session can be recovered, the poll-fallback gives up after `STRUCTURED_RUNS_SSE_UNKNOWN_TIMEOUT_S` with `structured.failed` (`structured_error: "run_not_found_upstream"`) instead of polling forever.
- `POST /v1/runs/structured/{run_id}/stop` — pass through to upstream run stop.
- `POST /v1/runs/structured/{run_id}/approval` — pass through approval responses.
- The finalizer first launches a **post-completion agent check in the same session**. The agent reviews the client JSON Schema and returns a corrected final answer before JSON extraction.
- `GET /v1/runs/structured/{run_id}/media?path=...` — serve attached image/video/audio artifacts safely.

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

Then the wrapper tries to finalize **the agent's own output** into schema-valid JSON:

- If that first pass is schema-valid **and not hollow** (see below) — and `jsonschema` is installed — the run completes there. `final_output_check` is `{"status":"skipped","reason":"first_pass_schema_valid"}` — no extra agent turn is spent.
- If it is **not** schema-valid, or is hollow, the wrapper runs a **post-completion agent turn in the same session**: the agent reviews the client JSON Schema and returns a corrected final answer, which is then finalized. `final_output_check` is `{"status":"completed","run_id":"run_yyy"}`.

**Hollow extraction:** a first pass can be schema-valid and still worthless — e.g. `content: {"type": "string"}` satisfied by `""`. Once the run's raw `output` is at least `STRUCTURED_RUNS_HOLLOW_EXTRACTION_MIN_SOURCE_CHARS` (default `500`) long, the finalizer requires the total extracted string content to be at least `STRUCTURED_RUNS_HOLLOW_EXTRACTION_MAX_RATIO` (default `0.1`, i.e. 10%) of that length, or the pass doesn't count as valid — this is what escalates it to the post-completion re-check above. If the result is still hollow after the re-check, the run ends at `structured_status: "failed"` with `structured_error: "finalizer_returned_hollow_extraction: parsed carried N of M source chars"` instead of silently completing on an empty result. `STRUCTURED_RUNS_FINAL_CHECK_MODE=off` is exempt — it always commits the first pass as-is (see below).

`STRUCTURED_RUNS_FINAL_CHECK_MODE` overrides this: `always` runs the agent turn on every completed run (legacy behavior); `off` never runs it and commits the first-pass result as-is (`{"status":"skipped","reason":"final_check_disabled"}`).

The wrapper resolves any artifact path mentioned in the reply against `STRUCTURED_RUNS_MEDIA_ROOTS` **before** the finalizer runs. Verified paths are passed to the agent (when the re-check runs) as authoritative absolute paths, so a follow-up is never dependent on the gateway's current working directory. The finalizer canonicalizes existing `*_path` values to bare absolute paths; `MEDIA:` remains a delivery marker, not the value stored in JSON.

If the agent turn cannot start, fails, or exceeds its deadline, the wrapper preserves the original completed output and reports `{"status":"fallback","error":"..."}` in `final_output_check`; it never silently drops a valid upstream result.

If the durable delegation state cannot be read (a locked `state.db` or a Hermes-core schema change), the settle step does **not** treat that as "nothing pending": it keeps polling until `STRUCTURED_RUNS_SESSION_SETTLE_TIMEOUT_S` and reports `session_settle.status = "timeout"`. A deployment with no `state.db` at all reports `"unavailable"` and skips the wait. A warning is logged when a query against `state.db` fails.

## Terminal structured state

A completed run always ends at a terminal `structured_status` — `completed`, `failed` or `skipped`. It is never left at `running` with nobody working on it, because a client cannot tell that apart from "result lost":

- Finalization runs as a **background task owned by the plugin event loop**, not by the request that started it. A client that hangs up mid-poll (or a poll that hits `STRUCTURED_RUNS_POLL_FINALIZE_WAIT_S`) cannot cancel it; the work continues and the next poll collects the result.
- The terminal upstream snapshot is persisted **at claim time**, before the settle / final-check awaits, so the completed output survives Hermes dropping the run record moments later.
- A finalizer that crashes or exceeds `STRUCTURED_RUNS_FINALIZER_MAX_RUNTIME_S` records `structured_status: "failed"` with a `structured_error` (`structured_finalizer_error: ...` / `structured_finalizer_timeout`).
- A `running` claim older than `STRUCTURED_RUNS_FINALIZER_STALE_AFTER_S` belongs to a process that died; the next poll reclaims and re-finalizes it. After `STRUCTURED_RUNS_FINALIZER_MAX_ATTEMPTS` the run fails terminally with `structured_finalizer_abandoned_after_N_attempts` rather than looping.

## Crash recovery

`finalize_structured` marks a run `structured_status = "running"` before its settle / final-check / finalizer steps. If the gateway restarts during that window, the wrapper rewinds any such run (that has not reached `structured_done`) back to `pending` on load, so the next poll re-runs the finalizer — unless it has already used up `STRUCTURED_RUNS_FINALIZER_MAX_ATTEMPTS`, in which case it is failed terminally.

## Retention and lost upstream records

Hermes keeps `/v1/runs` records only briefly, so `GET /v1/runs/{id}` can answer `404 run_not_found` while the structured result is still wanted. A run this wrapper still tracks is therefore never reported as `404`. The poll falls back, in order, to:

1. the terminal upstream snapshot persisted by this wrapper;
2. a session recovery read from Hermes `state.db`;
3. the wrapper's own structured record, returned as `status: "unknown"`, `last_event: "upstream_run_record_lost"`, with `upstream_status_unavailable: true` and the upstream error attached.

Only a run this wrapper has *also* forgotten yields `404`, and it does so with a distinct body so the client can tell an expired run from a bad id:

```json
{
  "error": {
    "message": "Run not found upstream and no structured record is retained for run_x. ...",
    "type": "invalid_request_error",
    "code": "structured_run_expired",
    "run_id": "run_x",
    "structured_retention_s": 604800
  }
}
```

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
| `STRUCTURED_RUNS_HOLLOW_EXTRACTION_MIN_SOURCE_CHARS` | `500` | Below this `output` length, a small/empty `parsed` is never flagged hollow — short answers are left alone. |
| `STRUCTURED_RUNS_HOLLOW_EXTRACTION_MAX_RATIO` | `0.1` | Minimum fraction of `output`'s length the extracted text must reach, once past the char floor above, or the pass counts as hollow. |
| `STRUCTURED_RUNS_FINAL_CHECK_MODE` | `auto` | When the post-completion agent re-check runs: `auto` = only when finalizing the agent's own output is not schema-valid or is hollow; `always` = every completed run (legacy); `off` = never. |
| `STRUCTURED_RUNS_FINAL_CHECK_TIMEOUT_S` | `120` | Maximum time for the post-completion agent correction turn. |
| `STRUCTURED_RUNS_FINAL_CHECK_POLL_INTERVAL_S` | `1` | Seconds between status checks for that correction turn. |
| `STRUCTURED_RUNS_SESSION_SETTLE_TIMEOUT_S` | `180` | Max wait for this session's background delegations/delivery before finalization. |
| `STRUCTURED_RUNS_SESSION_QUIET_S` | `3` | Required quiet window after delegated output delivery. |
| `STRUCTURED_RUNS_SESSION_SETTLE_POLL_INTERVAL_S` | `1` | Seconds between durable delegation-state checks. |
| `STRUCTURED_RUNS_SSE_UNKNOWN_TIMEOUT_S` | `90` | Grace period before the SSE poll-fallback emits `structured.failed` for a run that upstream has no record of and no recoverable session. |
| `STRUCTURED_RUNS_STATE_DB_BUSY_TIMEOUT_S` | `5` | SQLite busy timeout when reading Hermes `state.db` (it is written by Hermes core). |
| `STRUCTURED_RUNS_FINALIZER_MAX_RUNTIME_S` | settle + final-check + `300` | Hard cap on one finalization attempt. On expiry the run ends `failed` with `structured_finalizer_timeout` instead of staying `running`. |
| `STRUCTURED_RUNS_FINALIZER_STALE_AFTER_S` | `2 ×` max runtime | A `running` claim older than this is treated as abandoned (killed process) and may be reclaimed by the next poll. |
| `STRUCTURED_RUNS_FINALIZER_MAX_ATTEMPTS` | `3` | Finalization attempts before a run fails terminally with `structured_finalizer_abandoned_after_N_attempts`. |
| `STRUCTURED_RUNS_POLL_FINALIZE_WAIT_S` | `20` | How long a poll waits on an in-flight finalizer before answering `structured_status: "running"`. Finalization continues in the background either way. |
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
