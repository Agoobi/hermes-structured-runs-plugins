# Hermes Structured Runs Plugin

`structured-runs` is a Hermes Agent user plugin that wraps the existing `/v1/runs` API and adds a JSON Schema finalizer plus authenticated media routes.

It does **not** patch or replace Hermes core. It forwards work to the real Hermes API server, so the run still uses the normal Hermes agent loop and tools.

## What it adds

- `POST /v1/runs/structured` — create a normal Hermes run, plus `json_schema` for the final response.
- `GET /v1/runs/structured/{run_id}` — poll the upstream run and return `parsed` JSON once complete.
- `GET /v1/runs/structured/{run_id}/events` — proxy upstream SSE events, fall back to polling if upstream events are unavailable, and emit a final `structured.completed` / `structured.failed` event only when terminal.
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
  "structured_error": null
}
```

## Post-completion output check

Every successful structured run first waits for its own background delegations to complete and be delivered, then takes the newest persisted assistant reply from the same session. This avoids freezing an early final answer while QA or other `delegate_task` work is still returning.

The wrapper also resolves any artifact path mentioned in that reply against `STRUCTURED_RUNS_MEDIA_ROOTS` **before** starting the post-completion agent check. Verified paths are passed to the agent as authoritative absolute paths, so a later follow-up is never dependent on the gateway's current working directory. The finalizer canonicalizes existing `*_path` values to bare absolute paths; `MEDIA:` remains a delivery marker, not the value stored in JSON.

The wrapper exposes the result in `final_output_check`, for example:

```json
{"status":"completed","run_id":"run_xxx"}
```

If the follow-up cannot start, fails, or exceeds its deadline, the wrapper preserves the original completed output and reports `{"status":"fallback","error":"..."}` in `final_output_check`; it never silently drops a valid upstream result.

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
| `STRUCTURED_RUNS_FINAL_CHECK_TIMEOUT_S` | `120` | Maximum time for the post-completion agent correction turn. |
| `STRUCTURED_RUNS_FINAL_CHECK_POLL_INTERVAL_S` | `1` | Seconds between status checks for that correction turn. |
| `STRUCTURED_RUNS_SESSION_SETTLE_TIMEOUT_S` | `180` | Max wait for this session's background delegations/delivery before finalization. |
| `STRUCTURED_RUNS_SESSION_QUIET_S` | `3` | Required quiet window after delegated output delivery. |
| `STRUCTURED_RUNS_SESSION_SETTLE_POLL_INTERVAL_S` | `1` | Seconds between durable delegation-state checks. |
| `STRUCTURED_RUNS_MEDIA_ROOTS` | `/root/motion-graphic-templete,/root/.hermes,/tmp` | Comma-separated roots allowed for media serving. |

State is persisted at:

```text
~/.hermes/structured_runs_state.json
```

The state file stores schemas, structured results, and terminal upstream snapshots. It intentionally does **not** persist request `Authorization` headers.

## Security model

- All wrapper calls should use the same `Authorization: Bearer ...` header as the upstream Hermes API server.
- Cached structured results are **not** returned when upstream auth rejects the request (`401` / `403`).
- Media serving is locked down:
  - caller must pass upstream auth;
  - `run_id` must be known to the wrapper;
  - requested `path` must already be attached in the run's parsed JSON as a `*_path` field;
  - resolved file must be under `STRUCTURED_RUNS_MEDIA_ROOTS`;
  - `../` traversal is rejected.

## Limitations

- This is a wrapper on port `8646`, not a core patch to Hermes `/v1/runs` on port `8642`.
- The final structured JSON is produced by an additional `ctx.llm.complete_structured(...)` call after the upstream run completes.
- If you need direct browser media tags without auth headers, add signed temporary media URLs.
