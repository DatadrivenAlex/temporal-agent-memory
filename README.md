# Temporal Agent Memory

A small FastAPI service for storing evidence-backed facts without erasing how an agent learned or later corrected them. Each assertion keeps its source reference, confidence, valid-time range, and recorded time; corrections are append-only retraction events.

The API deliberately returns conflicting assertions together instead of guessing which one is true. A caller can ask what was valid on a date and what the system had recorded by a particular timestamp—useful primitives for auditable assistant memory and retrospective evaluation.

## Quick start

```bash
uv sync --extra test
uv run uvicorn temporal_memory.api:app --app-dir src --reload
```

The service creates `./memory.sqlite3` by default. Set `TEMPORAL_MEMORY_DB` to choose another path. Open `http://127.0.0.1:8000/docs` for the interactive API schema.

## Example

```bash
curl -s http://127.0.0.1:8000/facts \
  -H 'content-type: application/json' \
  -d '{"subject":"user:42","predicate":"preferred_language","value":"Python","source_ref":"conversation:msg-17","confidence":0.95,"valid_from":"2026-11-01","idempotency_key":"msg-17-language"}'

curl 'http://127.0.0.1:8000/facts?subject=user%3A42&valid_at=2026-11-15&recorded_before=2026-11-20T12%3A00%3A00Z'
```

`POST /facts/{fact_id}/retractions` adds a correction/retraction event; `GET /facts/{fact_id}/history` returns the append-only event history. Assertions with the same idempotency key and content return the original fact, while a changed payload returns `409`.

## Design notes

- `valid_from` / `valid_to` represent when a claim applies in the world; `recorded_at` represents when this service received it.
- `recorded_before` supports a bitemporal view. Retractions are effective only in views recorded at or after their event time.
- A valid-time range is half-open: `valid_from` is included and `valid_to` is excluded.
- A fact can have only one retraction event. To represent a revised belief, append a new assertion with its own source and validity range.
- Source references are opaque caller-supplied identifiers, not fetched or validated by this service.

## Checks

```bash
uv run ruff check .
uv run pytest -q
```

GitHub Actions runs lint and tests on Python 3.11, 3.12, and 3.13.

## Scope

This is a local SQLite reference implementation, not a hosted multi-tenant service. It does not extract facts with an LLM, authenticate callers, enforce access control, encrypt sensitive data, or decide whether a source is trustworthy. Do not store secrets or sensitive personal data in an unauthenticated deployment. It demonstrates storage/query semantics, not a production memory policy.
