from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator


class FactInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=200)
    predicate: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=4000)
    source_ref: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)
    valid_from: date
    valid_to: date | None = None
    idempotency_key: str = Field(min_length=1, max_length=200)

    @field_validator("valid_to")
    @classmethod
    def valid_range_is_forward(cls, value: date | None, info):
        valid_from = info.data.get("valid_from")
        if value is not None and valid_from is not None and value <= valid_from:
            raise ValueError("valid_to must be later than valid_from")
        return value


class RetractionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)
    idempotency_key: str = Field(min_length=1, max_length=200)


class Fact(BaseModel):
    id: str
    subject: str
    predicate: str
    value: str
    source_ref: str
    confidence: float
    valid_from: date
    valid_to: date | None
    recorded_at: datetime


class Event(BaseModel):
    event_id: str
    event_type: Literal["assert", "retract"]
    fact_id: str
    recorded_at: datetime
    payload: dict[str, object]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _connect(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def _initialize(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(db_path)) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL CHECK (event_type IN ('assert', 'retract')),
                fact_id TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                FOREIGN KEY (fact_id) REFERENCES facts(fact_id)
            );
            CREATE TABLE IF NOT EXISTS facts (
                fact_id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                value TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
                valid_from TEXT NOT NULL,
                valid_to TEXT,
                asserted_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS facts_lookup ON facts(subject, predicate, valid_from);
            CREATE INDEX IF NOT EXISTS events_fact_time ON events(fact_id, recorded_at);
            """
        )


def _fact(row: sqlite3.Row) -> Fact:
    return Fact(
        id=row["fact_id"],
        subject=row["subject"],
        predicate=row["predicate"],
        value=row["value"],
        source_ref=row["source_ref"],
        confidence=row["confidence"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        recorded_at=row["asserted_at"],
    )


def create_app(db_path: str | None = None) -> FastAPI:
    database = db_path or os.environ.get("TEMPORAL_MEMORY_DB", "./memory.sqlite3")
    _initialize(database)
    app = FastAPI(
        title="Temporal Agent Memory API",
        version="0.1.0",
        description="Append-only assertions and retractions with separate valid and recorded time.",
    )
    app.state.db_path = database

    @app.post("/facts", response_model=Fact, status_code=201)
    def assert_fact(body: FactInput):
        fact_id = str(uuid4())
        event_id = str(uuid4())
        timestamp = _utc_now()
        payload = body.model_dump(mode="json", exclude={"idempotency_key"})
        with closing(_connect(database)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM events WHERE idempotency_key = ?", (body.idempotency_key,)
            ).fetchone()
            if existing:
                if (
                    existing["event_type"] != "assert"
                    or json.loads(existing["payload_json"]) != payload
                ):
                    connection.rollback()
                    raise HTTPException(
                        409, "idempotency key was already used for different content"
                    )
                row = connection.execute(
                    "SELECT * FROM facts WHERE fact_id = ?", (existing["fact_id"],)
                ).fetchone()
                connection.commit()
                return _fact(row)
            connection.execute(
                "INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fact_id,
                    body.subject,
                    body.predicate,
                    body.value,
                    body.source_ref,
                    body.confidence,
                    body.valid_from.isoformat(),
                    body.valid_to.isoformat() if body.valid_to else None,
                    timestamp,
                ),
            )
            connection.execute(
                "INSERT INTO events VALUES (?, 'assert', ?, ?, ?, ?)",
                (
                    event_id,
                    fact_id,
                    timestamp,
                    json.dumps(payload, sort_keys=True),
                    body.idempotency_key,
                ),
            )
            row = connection.execute("SELECT * FROM facts WHERE fact_id = ?", (fact_id,)).fetchone()
            connection.commit()
        return _fact(row)

    @app.get("/facts", response_model=list[Fact])
    def list_facts(
        subject: str | None = None,
        predicate: str | None = None,
        valid_at: date | None = None,
        recorded_before: datetime | None = None,
    ):
        valid_at = valid_at or datetime.now(UTC).date()
        if recorded_before is not None and recorded_before.tzinfo is None:
            raise HTTPException(422, "recorded_before must include a timezone")
        recorded_limit = (
            recorded_before.astimezone(UTC).isoformat() if recorded_before else _utc_now()
        )
        conditions = [
            "f.valid_from <= ?",
            "(f.valid_to IS NULL OR f.valid_to > ?)",
            "a.recorded_at <= ?",
        ]
        parameters: list[object] = [valid_at.isoformat(), valid_at.isoformat(), recorded_limit]
        if subject is not None:
            conditions.append("f.subject = ?")
            parameters.append(subject)
        if predicate is not None:
            conditions.append("f.predicate = ?")
            parameters.append(predicate)
        conditions.append(
            "NOT EXISTS (SELECT 1 FROM events r WHERE r.event_type = 'retract' "
            "AND r.fact_id = f.fact_id AND r.recorded_at <= ?)"
        )
        parameters.append(recorded_limit)
        sql = f"""
            SELECT f.*
            FROM facts f
            JOIN events a ON a.fact_id = f.fact_id AND a.event_type = 'assert'
            WHERE {" AND ".join(conditions)}
            ORDER BY f.subject, f.predicate, f.valid_from, f.fact_id
        """
        with closing(_connect(database)) as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [_fact(row) for row in rows]

    @app.post("/facts/{fact_id}/retractions", response_model=Event, status_code=201)
    def retract_fact(fact_id: str, body: RetractionInput):
        event_id = str(uuid4())
        timestamp = _utc_now()
        payload = {"reason": body.reason, "fact_id": fact_id}
        with closing(_connect(database)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM events WHERE idempotency_key = ?", (body.idempotency_key,)
            ).fetchone()
            if existing:
                if (
                    existing["event_type"] != "retract"
                    or json.loads(existing["payload_json"]) != payload
                ):
                    connection.rollback()
                    raise HTTPException(
                        409, "idempotency key was already used for different content"
                    )
                connection.commit()
                return Event(
                    event_id=existing["event_id"],
                    event_type="retract",
                    fact_id=fact_id,
                    recorded_at=existing["recorded_at"],
                    payload=payload,
                )
            fact = connection.execute(
                "SELECT 1 FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            if not fact:
                connection.rollback()
                raise HTTPException(404, "fact not found")
            already_retracted = connection.execute(
                "SELECT 1 FROM events WHERE event_type = 'retract' AND fact_id = ?", (fact_id,)
            ).fetchone()
            if already_retracted:
                connection.rollback()
                raise HTTPException(409, "fact already has a retraction event")
            connection.execute(
                "INSERT INTO events VALUES (?, 'retract', ?, ?, ?, ?)",
                (
                    event_id,
                    fact_id,
                    timestamp,
                    json.dumps(payload, sort_keys=True),
                    body.idempotency_key,
                ),
            )
            connection.commit()
        return Event(
            event_id=event_id,
            event_type="retract",
            fact_id=fact_id,
            recorded_at=timestamp,
            payload=payload,
        )

    @app.get("/facts/{fact_id}/history", response_model=list[Event])
    def fact_history(fact_id: str):
        with closing(_connect(database)) as connection:
            if not connection.execute(
                "SELECT 1 FROM facts WHERE fact_id = ?", (fact_id,)
            ).fetchone():
                raise HTTPException(404, "fact not found")
            rows = connection.execute(
                "SELECT * FROM events WHERE fact_id = ? ORDER BY recorded_at, event_id", (fact_id,)
            ).fetchall()
        return [
            Event(
                event_id=row["event_id"],
                event_type=row["event_type"],
                fact_id=row["fact_id"],
                recorded_at=row["recorded_at"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    return app


app = create_app()
