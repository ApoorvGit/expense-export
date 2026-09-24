# expense-report

A FastAPI service that turns bank transaction SMS into a queryable expense log.
An iOS Shortcuts automation forwards each debit message to it; the service parses
out the amount, merchant, bank, instrument and category, and stores it in
Postgres — so every spend is recorded without manual entry.

Live: `https://expense-export.onrender.com`

---

## How the pieces fit

```
iPhone (Shortcuts automation, triggered by bank SMS)
   │  1. GET /ping          — wakes the sleeping container
   │  2. POST /entries      — X-API-Key header + {message, date}
   ▼
Render (free web service, sleeps after ~15 min idle)
   │  parses amount / merchant / bank / instrument / category
   │  psycopg connection pool over TLS
   ▼
Neon Postgres (ap-southeast-1) — the durable bit
   ▲
   └── a web frontend reads /entries and corrects categories via PATCH
```

The database is deliberately **not** on Render. Render's free disks are wiped on
every restart, and its free Postgres expires after 30 days. Neon's free tier has
no expiry, so the data outlives the web service.

---

## Data model

One table, `entries`. Only `message` and `date` are supplied by the Shortcut;
everything else is parsed from the message text on insert.

| Column | Type | Notes |
|---|---|---|
| `id` | `SERIAL PRIMARY KEY` | Insert order |
| `message` | `TEXT NOT NULL` | The raw bank SMS, verbatim — the source of truth |
| `date` | `DATE NOT NULL` | Date only, no time |
| `amount` | `NUMERIC(12,2)` | Parsed. The transaction, never the quoted balance |
| `currency` | `TEXT` | Parsed. `INR` / `USD` / `EUR` |
| `merchant` | `TEXT` | Parsed, upper-cased so the same shop groups as one |
| `direction` | `TEXT` | Parsed. `debit` or `credit` |
| `instrument` | `TEXT` | Parsed. `credit_card`, `upi`, `ach`, `standing_instruction`, `account`, ... |
| `bank` | `TEXT` | Parsed. `HDFC`, `Axis`, `ICICI`, `IDBI`, ... |
| `category` | `TEXT` | One of 26 values from `GET /categories` |
| `category_source` | `TEXT` | `auto` when parsed, `manual` after a `PATCH` |
| `note` | `TEXT` | Free-text, set via `PATCH`. Never parsed from the SMS |

**Every parsed column is nullable.** An unrecognised bank template, or a
promotional SMS that slipped past the Shortcut's filter, stores fine and leaves
them null. `message` always holds the original, so any parser improvement can
re-process history.

Three things worth knowing about the numbers:

- **`direction` is always `debit` in practice.** The automation only fires on
  messages containing "sent" or "debited", so credits never arrive. The column is
  a safety net if that filter is ever widened. ("Spent" contains "sent", which is
  how card messages qualify.)
- **`instrument` is what makes totals honest.** `ach` and `standing_instruction`
  rows are transfers and auto-debits, not discretionary spending, and they dwarf
  everything else when lumped in.
- **A `manual` category always wins.** Re-parsing updates amount and merchant but
  never overwrites a category a human set.

---

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/ping` | none | Wake the container. Touches no database. |
| `POST` | `/entries` | `X-API-Key` | Store one entry. |
| `PATCH` | `/entries/{id}` | `X-API-Key` | Set or clear one entry's `category` and/or `note`. Only fields present in the body are touched; setting `category` marks it `manual`. |
| `DELETE` | `/entries/{id}` | `X-API-Key` | Delete an entry. Returns the removed row so it can be re-posted. |
| `GET` | `/categories` | `X-API-Key` | The flat list of category values, in picker order. |
| `POST` | `/categories` | `X-API-Key` | Add a new category with keywords (`{"name": "...", "keywords": [...]}`). Persisted in `categories`/`category_keywords`; the keywords make it auto-detected on future entries. |
| `GET` | `/entries` | `X-API-Key` | List entries, newest first. Supports `since`, `until`, `direction`, `instrument`, `bank`, `category`, `limit`, `offset`, `order`. |

`POST` body — the `date` field is forgiving (see [Date handling](#date-handling)):

```json
{ "message": "INR 358.00 debited ... Bal INR 1466.76", "date": "2026-08-31" }
```

Returns `201` with the created row. Returns `401` without a valid key, `422` if
the body can't be parsed.

### Using it from the command line

```bash
export EXPENSE_KEY='...'   # the API_KEY value from Render
BASE=https://expense-export.onrender.com

# everything, newest first
curl -sS $BASE/entries -H "X-API-Key: $EXPENSE_KEY" | python3 -m json.tool

# one month of card spending only
curl -sS "$BASE/entries?since=2026-08-01&until=2026-08-31&instrument=credit_card" \
  -H "X-API-Key: $EXPENSE_KEY" | python3 -m json.tool

# how many rows failed to parse
curl -sS $BASE/entries -H "X-API-Key: $EXPENSE_KEY" | python3 -c \
  "import json,sys; r=json.load(sys.stdin); print(sum(1 for e in r if e['amount'] is None), 'of', len(r), 'unparsed')"

# the category picker values
curl -sS $BASE/categories -H "X-API-Key: $EXPENSE_KEY"

# add a new category — keywords make it auto-detected on future entries
curl -sS -X POST $BASE/categories -H "X-API-Key: $EXPENSE_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"side hustle","keywords":["FREELANCE CLIENT","UPWORK"]}'

# categorise entry 7
curl -sS -X PATCH $BASE/entries/7 -H "X-API-Key: $EXPENSE_KEY" \
  -H 'Content-Type: application/json' -d '{"category":"rent"}'

# add a note to entry 7, without touching its category
curl -sS -X PATCH $BASE/entries/7 -H "X-API-Key: $EXPENSE_KEY" \
  -H 'Content-Type: application/json' -d '{"note":"split with roommate, owes 50%"}'

# delete entry 7 (returns the row, so you can re-post it to undo)
curl -sS -X DELETE $BASE/entries/7 -H "X-API-Key: $EXPENSE_KEY"
```

Deletion is permanent — there is no soft delete, so keep the response if you
might want the row back.

---

## Environment variables

`DATABASE_URL` and `API_KEY` are **required** — the app refuses to start without
them, so a misconfigured deploy fails loudly instead of serving bank messages
publicly. `ALLOWED_ORIGINS` is optional.

| Variable | Where it's set | Value |
|---|---|---|
| `DATABASE_URL` | Render → Environment | Neon pooled connection string, including `?sslmode=require` |
| `API_KEY` | Render → Environment | Long random string, see below |
| `ALLOWED_ORIGINS` | Render → Environment | Optional. Comma-separated CORS origins; defaults to `*` |

Neither secret belongs in this repo. `render.yaml` marks `DATABASE_URL` as
`sync: false` for exactly this reason.

Generate an API key:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

---

## Setting this up somewhere else

### 1. Database (Neon)

1. Create a project at [neon.tech](https://neon.tech), region close to you.
2. Copy the **pooled** connection string — the host contains `-pooler`.
3. Keep the `?sslmode=require` query parameters intact; Neon rejects
   unencrypted connections.

The `entries` table and its columns are created on startup, idempotently —
`CREATE TABLE IF NOT EXISTS` plus `ADD COLUMN IF NOT EXISTS` for each column
added later. Rows stored before a column existed are backfilled by re-parsing
their `message`. There is no migration tool to run, and restarting is always
safe.

### 2. Web service (Render)

New → **Web Service**, connect the repo, then:

| Field | Value |
|---|---|
| Language | Python 3 |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| Instance Type | Free |

`--host 0.0.0.0 --port $PORT` is mandatory. Binding to localhost or a fixed port
makes Render's health check fail.

**Add both environment variables before the first deploy**, or startup crashes
with `KeyError`.

`render.yaml` is only read for Blueprint deploys. If you create the service
through the dashboard it is ignored, and it's fine to leave it as documentation.

### Alternative to Render: self-hosting with Docker

Avoids cold starts entirely and sidesteps Render's shared 750 free
instance-hour/month budget — a reasonable option if you already run
something like a home NAS.

```bash
cp .env.example .env   # fill in DATABASE_URL and API_KEY
docker compose up -d --build
```

That builds the image from the `Dockerfile`, reads `DATABASE_URL`/`API_KEY`
(and optional `ALLOWED_ORIGINS`) from `.env`, publishes port `8000`, and sets
`restart: unless-stopped` so it comes back after a reboot or crash — as long
as Docker itself is set to start on boot. Still point `DATABASE_URL` at Neon
(or any reachable Postgres); nothing about the app changes when self-hosted.

If the box is only reachable over Tailscale (e.g. a NAS), the iOS Shortcut
needs a way in:

- **Tailscale on the iPhone too** — join the same tailnet, use `tailscale
  cert` for a real HTTPS cert on the box, and point the Shortcut at the
  device's MagicDNS name. Private, but the Shortcut fails silently if
  Tailscale isn't connected on the phone.
- **`tailscale funnel`** — exposes just this service on a public
  `https://<device>.<tailnet>.ts.net` URL through Tailscale's relay, so the
  phone doesn't need to be on the tailnet at all. Free on the Personal plan.

Without Docker, the same image's `CMD` — `uvicorn main:app --host 0.0.0.0
--port 8000` — is exactly what you'd run under a systemd unit instead; Docker
just makes the restart-on-boot and dependency isolation someone else's
problem.

### 3. The iOS Shortcut

An automation triggered on messages from your bank, with three actions:

1. **Get Contents of URL**
   - `GET https://<your-service>/ping`
   - Result unused. This wakes the container; the request only returns once the
     app is up, which is why no long wait is needed afterwards.
2. **Wait** — 3 seconds (optional safety margin)
3. **Get Contents of URL**
   - `POST https://<your-service>/entries`
   - Headers: `Content-Type: application/json`, `X-API-Key: <your key>`
   - Request Body: **JSON**, or **File** with a prebuilt dictionary — both work
   - Fields: `message` (the SMS text), `date`

Keep a **Quick Look** action after the POST while testing. It shows the created
row on success and the exact error on failure, which is far faster than reading
server logs.

### 4. Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# throwaway Postgres
docker run -d --rm --name pg -e POSTGRES_PASSWORD=t -e POSTGRES_DB=e \
  -p 5432:5432 postgres:16-alpine

export DATABASE_URL="postgresql://postgres:t@localhost:5432/e"
export API_KEY="dev-key"
.venv/bin/uvicorn main:app --reload
```

Docs at `http://127.0.0.1:8000/docs`.

---

## Date handling

iOS renders its Date variable as `31 Aug 2026 at 4:56 PM`, using a **narrow
no-break space** (U+202F) before the meridiem. A plain Pydantic `date` field
rejects that, which is what silently broke the automation initially.

`coerce_date` in `main.py` normalises the input first: it folds exotic Unicode
spaces to plain ones, strips a trailing ` at <time>`, then tries ISO parsing
followed by a list of common formats. Accepted forms include:

```
2026-08-31            2026-08-31T14:22:05Z     31 Aug 2026 at 4:56 PM
31 Aug 2026           August 31, 2026          1 September 2026 at 09:05
31/08/2026            31.08.2026               Aug 31, 2026
```

Slash-separated dates are read **day-first**, so `01/02/2026` is 1 February.

`POST /entries` also reads the raw body and parses JSON regardless of
`Content-Type`, and will pull the object out of a multipart-wrapped body. This
exists because Shortcuts sends a non-JSON content type in File mode.

---

## Gotchas worth remembering

**Cold starts.** The free Render instance sleeps after ~15 minutes idle and takes
about **22 seconds** to wake (measured). Render holds the request open while
booting rather than rejecting it, so writes still succeed — that's within iOS's
~60s timeout, and the `/ping` step keeps it off the write path. If you ever need
this gone, Starter (~$7/mo) removes spin-down entirely.

**Free instance hours.** Render allows 750/month. A month is ~730 hours, so an
always-awake keep-alive ping consumes nearly all of it. Don't run a second free
service alongside one.

**Don't point a keep-alive at `/entries`.** It queries Postgres, so pinging it
holds Neon's compute awake too and burns its free compute budget. `/ping` exists
to avoid that.

**Logs never contain SMS content.** Rejected requests log the payload's keys, the
`date` value and `message_chars=<n>` — not the message. Pydantic's default error
string embeds the whole input, so `describe_error` strips values out. Keep it
that way; these messages carry account tails, balances and UPI references.

**Auth is a shared secret, not authorization.** Anyone holding the key has full
read and write access. Fine for one automation on your own phone; not fine if
you share the Shortcut or add users.

**`/docs` and `/openapi.json` are public.** Schema only, no data. Close them with
`FastAPI(..., docs_url=None, openapi_url=None)` if you'd rather not advertise the
shape.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `401` | `X-API-Key` missing, or doesn't match Render's `API_KEY` |
| `422`, log says `date: date_from_datetime_parsing` | Date format not recognised — the log prints the value it received |
| `422`, log says `unparsed bytes=<n>` | Body wasn't JSON at all |
| `422`, log says `message: missing` | Shortcut isn't sending the `message` field |
| Startup `KeyError: 'DATABASE_URL'` / `'API_KEY'` | Env var missing in Render |
| First request hangs ~22s then succeeds | Normal cold start |
| `GET /entries` hangs then 500s | Neon unreachable — check the connection string kept its query parameters |
| Entries vanished after a restart | `DATABASE_URL` isn't set, so it fell back to something ephemeral. Should be impossible now: the app requires the variable. |

Render's logs are under **Logs** in the service dashboard. Rejected POSTs appear
as `rejected POST /entries: ...`.

---

## Rotating credentials

**Neon password:** Neon → Roles → `neondb_owner` → Reset password, then update
`DATABASE_URL` in Render. It redeploys automatically.

**API key:** generate a new one, update `API_KEY` in Render, then update the
`X-API-Key` header in the Shortcut. Do the Shortcut promptly — entries fail with
`401` in between.

---

## Files

| File | Purpose |
|---|---|
| `main.py` | The entire service |
| `requirements.txt` | `fastapi`, `uvicorn`, `psycopg[binary]`, `psycopg-pool` |
| `render.yaml` | Blueprint config; ignored by dashboard-created services |
| `Dockerfile` | Builds the service into an image; used for self-hosting |
| `docker-compose.yml` | Runs that image with `.env`, port `8000`, auto-restart |
| `.env.example` | Template for the env file `docker-compose.yml` reads |
| `.gitignore` | Excludes `.venv/`, `__pycache__/`, `*.db`, `.env` |

Never commit `.venv/`, `__pycache__/`, `*.db`, or `.env`. An old `entries.db`
from the pre-Postgres version may contain real bank SMS, and `.env` holds
`DATABASE_URL`/`API_KEY` once you fill it in.

## Possible next steps

- No `PATCH`/`DELETE`, so fixing a mis-parsed or duplicate entry means going into
  Neon's SQL editor. Most likely the next thing worth adding.
- `PATCH` corrects one row but doesn't teach the parser, so a recurring payee
  needs recategorising each time. A merchant-to-category override table
  consulted at insert time would fix that.
- `PATCH` only accepts `category` and `note`. Fixing a wrong amount or date
  means deleting the row and re-posting a corrected message, or editing it in
  Neon.
- No aggregation endpoint; totals are computed from fetched rows.
- `date` has no time component, so same-day ordering falls back to `id`.

`BACKEND_SPEC.md` is the handoff document for a frontend session — it covers the
API contract, the nullable-column semantics and the security constraints.
