import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ValidationError, field_validator

log = logging.getLogger("uvicorn.error")

# Override with DB_PATH in deployment to point at a persistent disk mount.
DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).parent / "entries.db"))


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT NOT NULL,
                date TEXT NOT NULL
            )
            """
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Entries Service", lifespan=lifespan)


DATE_FORMATS = (
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d %B %Y",
    "%B %d, %Y",
    "%d %b %Y",
)


class EntryIn(BaseModel):
    message: str
    date: date

    @field_validator("date", mode="before")
    @classmethod
    def coerce_date(cls, value):
        """Accept timestamps and common human-readable dates, not just YYYY-MM-DD.

        iOS Shortcuts serializes its Date variable as a full ISO timestamp, which
        a bare `date` field rejects.
        """
        if isinstance(value, datetime):
            return value.date()
        if not isinstance(value, str):
            return value
        text = value.strip()
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            pass
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        return value


class Entry(EntryIn):
    id: int


def extract_json(raw: bytes) -> dict:
    """Pull a JSON object out of a request body whatever the content type.

    Clients that post the payload as a file send it raw or wrapped in multipart
    MIME boundaries; both carry the object we want.
    """
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


@app.post("/entries", response_model=Entry, status_code=201)
async def create_entry(request: Request) -> Entry:
    raw = await request.body()
    try:
        payload = extract_json(raw)
        entry = EntryIn.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, UnicodeDecodeError) as exc:
        log.warning(
            "rejected POST /entries: content-type=%r body=%r error=%s",
            request.headers.get("content-type"),
            raw[:1000],
            exc,
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO entries (message, date) VALUES (?, ?)",
            (entry.message, entry.date.isoformat()),
        )
    return Entry(id=cur.lastrowid, message=entry.message, date=entry.date)


@app.get("/entries", response_model=list[Entry])
def list_entries() -> list[Entry]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, message, date FROM entries ORDER BY id"
        ).fetchall()
    return [Entry(**dict(row)) for row in rows]
