# PageWatch architecture

PageWatch is two (three) processes that share one data folder:

```
pagewatch-engine  (always on, in the user's session; the only writer, the only network client)
   ├─ asyncio event loop: scheduler, fetchers, local API, action jobs
   ├─ writer thread: owns the single SQLite write connection
   ├─ reader threads: read-only SQLite connections
   ├─ worker processes (spawn): parse → filter → hash → diff → gate
   └─ tray thread (Windows)
pagewatch-ui      (on demand; PySide6; a client of the local API)
pagewatch-cli     (scripting; a client of the local API)
```

Data folder (`%LOCALAPPDATA%\PageWatch`, or `--data-dir`):
`pagewatch.db` (WAL), `blobs/ab/cd/<sha256>.zst`, `profiles/`, `logs/engine.log`,
`backups/`, `plugins/`, `engine.lock`, `engine.mutex`.

## Layers (as built)

| Module | Role |
| --- | --- |
| `models.py` | Pydantic models: every JSON column and every API body. No engine imports. |
| `engine/clock.py` | `Clock` protocol, `SystemClock`, `FakeClock`. No other module reads time directly. |
| `engine/paths.py` | `DataDir` layout and `--data-dir` / env / platform default resolution. |
| `engine/logs.py` | structlog JSON logs, rotating file (10 MB × 5), secret redaction, runtime DEBUG toggle. |
| `engine/store/db.py` | Migrations (one transaction, backup first); `Database` = writer thread + reader pool. |
| `engine/store/blobs.py` | Content-addressed, zstd-compressed, atomic blob store; safe across processes. |
| `engine/workers.py` | Process pool (spawn) with a thread-mode twin for tests; rebuilds after a crash. |
| `engine/settings.py` | Global settings: one row per changed key in `setting`. |
| `engine/instance.py` | Single-instance guard (named mutex / flock) and `engine.lock`. |
| `engine/core.py` | `Engine`: owns db, blobs, pool, event bus, settings; lifecycle and `/health`. |
| `engine/api/` | FastAPI app, ASGI guard (token, no Origin, loopback Host), `/events` WebSocket. |
| `engine/main.py` | Entry point `pagewatch-engine`: guard → logging → engine → API → lockfile → wait. |

## Rules that hold everywhere

* The event loop never runs `sqlite3` calls, parsing, hashing or diffing.
* Each check's writes are one `BEGIN IMMEDIATE … COMMIT` on the writer thread.
* A blob is written (atomic rename) **before** any row references it.
* Times are stored as fixed-width UTC strings (`2026-10-05T13:10:00.000000Z`) so string order is time order.
* Only the engine writes data. The UI and CLI go through the API.

## Security model

The API binds `127.0.0.1` on a random port. Every request needs `Authorization: Bearer <token>`
(token regenerated at every start, in `engine.lock`, owner-only). Requests with an `Origin`
header or a non-loopback `Host` are rejected. WebSockets send the token in their first message.
