import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

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


class EntryIn(BaseModel):
    message: str
    date: date


class Entry(EntryIn):
    id: int


@app.post("/entries", response_model=Entry, status_code=201)
def create_entry(entry: EntryIn) -> Entry:
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
