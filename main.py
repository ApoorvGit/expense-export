import json
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ValidationError, field_validator

log = logging.getLogger("uvicorn.error")

DATABASE_URL = os.environ["DATABASE_URL"]

# Read at import so a deployment missing the key fails to start rather than
# serving bank messages to the open internet.
API_KEY = os.environ["API_KEY"]


def require_api_key(x_api_key: str = Header(default="")) -> None:
    """Gate an endpoint on the shared secret sent as the X-API-Key header."""
    if not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key")

# Free-tier Postgres auto-suspends its compute when idle, which silently kills
# pooled connections. `check` validates each one on checkout and transparently
# replaces the dead ones, so a request after a quiet spell wakes the database
# instead of failing on a stale socket.
pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=4,
    open=False,
    timeout=30,
    max_idle=300,
    check=ConnectionPool.check_connection,
)


def init_db() -> None:
    with pool.connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entries (
                id SERIAL PRIMARY KEY,
                message TEXT NOT NULL,
                date DATE NOT NULL
            )
            """
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open(wait=True, timeout=30)
    init_db()
    yield
    pool.close()


app = FastAPI(title="Entries Service", lifespan=lifespan)


@app.get("/ping")
def ping() -> dict:
    """Wake the container without touching the database.

    Free hosting suspends the service when idle; a request to this endpoint is
    held open while it boots, so a client that pings first meets a warm server
    on its next call. Deliberately does no query, leaving the database
    suspended until an actual entry arrives.
    """
    return {"status": "ok"}


DATE_FORMATS = (
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d %B %Y",
    "%B %d, %Y",
    "%d %b %Y",
    "%b %d, %Y",
    "%d.%m.%Y",
)

# Unicode spaces iOS uses in formatted dates, plus the " at <time>" suffix it
# appends. Both have to go before the date itself will parse.
UNICODE_SPACES = (" ", " ", " ", " ")
TIME_SUFFIX = re.compile(r"[\s,]+(?:at|kl\.?|um|à)\s+.*$", re.IGNORECASE)


def normalize_date_text(value: str) -> str:
    """Reduce a localised date string to just its date portion."""
    text = value.strip()
    for space in UNICODE_SPACES:
        text = text.replace(space, " ")
    text = TIME_SUFFIX.sub("", text)
    return " ".join(text.split())


class EntryIn(BaseModel):
    message: str
    date: date

    @field_validator("date", mode="before")
    @classmethod
    def coerce_date(cls, value):
        """Accept timestamps and common human-readable dates, not just YYYY-MM-DD.

        iOS Shortcuts renders its Date variable as "31 Aug 2026 at 4:56 PM",
        using a narrow no-break space before the meridiem; a bare `date` field
        rejects that and every other localised form.
        """
        if isinstance(value, datetime):
            return value.date()
        if not isinstance(value, str):
            return value
        text = normalize_date_text(value)
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


def describe_payload(raw: bytes, payload: object) -> str:
    """Summarise a rejected body without echoing the entry itself.

    Messages are bank SMS carrying account tails, balances and transaction
    references, and logs are retained far longer than the debugging is useful
    for. Record the payload's shape and its date, which is the field that
    actually fails, and reduce the message to a length.
    """
    if isinstance(payload, dict):
        parts = [f"keys={sorted(map(str, payload))}"]
        if "date" in payload:
            parts.append(f"date={payload['date']!r}")
        if isinstance(payload.get("message"), str):
            parts.append(f"message_chars={len(payload['message'])}")
        return " ".join(parts)
    return f"unparsed bytes={len(raw)}"


def describe_error(exc: Exception) -> str:
    """Render a validation failure without its input values.

    Pydantic's own string embeds the offending input, which for a missing field
    is the entire payload.
    """
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'body'}: {err['type']}"
            for err in exc.errors()
        )
    return f"{type(exc).__name__}: {exc}"


@app.post(
    "/entries",
    response_model=Entry,
    status_code=201,
    dependencies=[Depends(require_api_key)],
)
async def create_entry(request: Request) -> Entry:
    raw = await request.body()
    payload: object = None
    try:
        payload = extract_json(raw)
        entry = EntryIn.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, UnicodeDecodeError) as exc:
        log.warning(
            "rejected POST /entries: content-type=%r %s error=%s",
            request.headers.get("content-type"),
            describe_payload(raw, payload),
            describe_error(exc),
        )
        # The caller owns this data, so the response may name values the log omits.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO entries (message, date) VALUES (%s, %s) RETURNING id",
            (entry.message, entry.date),
        ).fetchone()
    return Entry(id=row[0], message=entry.message, date=entry.date)


@app.get(
    "/entries",
    response_model=list[Entry],
    dependencies=[Depends(require_api_key)],
)
def list_entries() -> list[Entry]:
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            "SELECT id, message, date FROM entries ORDER BY id"
        ).fetchall()
    return [Entry(**row) for row in rows]
