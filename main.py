import json
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ValidationError, field_validator

log = logging.getLogger("uvicorn.error")

DATABASE_URL = os.environ["DATABASE_URL"]

# Read at import so a deployment missing the key fails to start rather than
# serving bank messages to the open internet.
API_KEY = os.environ["API_KEY"]

# Comma-separated origins, or "*". A server-rendered frontend calling this from
# its own backend needs no CORS at all; this exists for browser-side clients.
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]


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


# --------------------------------------------------------------------------
# Parsing spend details out of the raw SMS
# --------------------------------------------------------------------------

CURRENCY_BY_TOKEN = {
    "inr": "INR",
    "rs": "INR",
    "rs.": "INR",
    "₹": "INR",
    "usd": "USD",
    "$": "USD",
    "eur": "EUR",
    "€": "EUR",
}

AMOUNT_RE = re.compile(
    r"(?P<cur>INR\.?|Rs\.?|₹|USD|\$|EUR|€)\s*(?P<amt>\d[\d,]*(?:\.\d{1,2})?)",
    re.IGNORECASE,
)

# Every SMS also quotes a balance or remaining card limit. Amounts introduced by
# one of those words are not the transaction, so they must not win. The window is
# wide and digit-free so it spans "Bal (incl. of chq in clg) INR. 7520.76".
BALANCE_LEAD_IN = re.compile(
    r"(bal|balance|avl|avbl|available|lmt|limit)[^0-9]{0,30}$", re.IGNORECASE
)

# Terminator shared by the merchant patterns: stop at a clause break or a
# trailing keyword, but not at a dot inside a name like RENDER.COM.
_MERCHANT_END = (
    r"(?=[;,\n]|\.\s|\.$|\s+(?:on|via|thru|through|upi|ref|refno|txn)\b|$)"
)

# Tried in order; the first candidate that survives validation wins.
MERCHANT_PATTERNS = (
    # "At TATA 1MG HEALTHCARE On ..." / "To K ABHIJITH" / "at AMAZON PAY IN B."
    re.compile(rf"\b(?:to|at)\s+(?P<merchant>[^;,\n]{{2,60}}?)\s*{_MERCHANT_END}", re.I),
    # "Info: ACH D- PUNJAB NATIONAL BANK-..." / "Det:APY SI DEDUCTION-..."
    re.compile(rf"\b(?:det|details|info)\s*:\s*(?P<merchant>[^\n]{{2,60}}?)\s*{_MERCHANT_END}", re.I),
    # "on 29-Aug-26 on AMAZON PAY IN G." — leading letter keeps dates out.
    re.compile(rf"\bon\s+(?P<merchant>[A-Za-z][^;,\n]{{1,58}}?)\s*{_MERCHANT_END}", re.I),
)

# Axis puts the merchant alone on the line above the remaining-limit line.
LIMIT_LINE = re.compile(r"^\s*(?:avl|available)\b", re.IGNORECASE)

# "credited to your a/c 8006" yields a merchant that is really the user's own
# account, which is noise in a spending list.
ACCOUNT_LIKE = re.compile(r"\ba/?c\b|\baccount\b|^your\b", re.IGNORECASE)

# Boilerplate that follows the useful part of every one of these messages.
MERCHANT_NOISE = re.compile(
    r"\b(block|reissue|dispute|convert|emi|missed|know more|not you|call|sms)\b",
    re.IGNORECASE,
)

# How the money moved. Ordered: the first pattern that matches wins, because a
# UPI message also names an account and a card message also names a limit.
INSTRUMENT_PATTERNS = (
    ("ach", re.compile(r"\bACH\b", re.IGNORECASE)),
    (
        "standing_instruction",
        re.compile(r"\bSI\b|\bstanding instruction\b|\be-?mandate\b", re.IGNORECASE),
    ),
    ("neft", re.compile(r"\bNEFT\b", re.IGNORECASE)),
    ("imps", re.compile(r"\bIMPS\b", re.IGNORECASE)),
    ("upi", re.compile(r"\bUPI\b|\bVPA\b", re.IGNORECASE)),
    ("atm", re.compile(r"\bATM\b|\bcash withdrawal\b", re.IGNORECASE)),
    # A remaining credit limit, or a "block CC" helpline, only appear on cards.
    (
        "credit_card",
        re.compile(
            r"\bavl\s*(?:limit|lmt)\b|\bavailable\s*limit\b|\bblock\s*cc\b"
            r"|\bcredit\s*card\b",
            re.IGNORECASE,
        ),
    ),
    ("debit_card", re.compile(r"\bdebit\s*card\b", re.IGNORECASE)),
    ("card", re.compile(r"\bcard\b", re.IGNORECASE)),
    # Fallback: the account itself was debited, e.g. a bank fee.
    ("account", re.compile(r"\ba/?c\b|\baccount\b", re.IGNORECASE)),
)


def find_instrument(message: str) -> str | None:
    for name, pattern in INSTRUMENT_PATTERNS:
        if pattern.search(message):
            return name
    return None


BANK_PATTERNS = (
    ("HDFC", re.compile(r"\bHDFC\b", re.IGNORECASE)),
    ("ICICI", re.compile(r"\bICICI\b", re.IGNORECASE)),
    ("Axis", re.compile(r"\bAXIS\b", re.IGNORECASE)),
    ("IDBI", re.compile(r"\bIDBI\b", re.IGNORECASE)),
    ("IDFC", re.compile(r"\bIDFC\b", re.IGNORECASE)),
    ("SBI", re.compile(r"\bSBI\b|\bState Bank\b", re.IGNORECASE)),
    ("Kotak", re.compile(r"\bKOTAK\b", re.IGNORECASE)),
    ("DCB", re.compile(r"\bDCB\b", re.IGNORECASE)),
    ("PNB", re.compile(r"\bPNB\b|\bPUNJAB NATIONAL BANK\b", re.IGNORECASE)),
    ("Yes Bank", re.compile(r"\bYES BANK\b", re.IGNORECASE)),
    ("IndusInd", re.compile(r"\bINDUSIND\b", re.IGNORECASE)),
    ("Bank of Baroda", re.compile(r"\bBANK OF BARODA\b|\bBOB\b", re.IGNORECASE)),
    ("Canara", re.compile(r"\bCANARA\b", re.IGNORECASE)),
    ("Federal", re.compile(r"\bFEDERAL BANK\b", re.IGNORECASE)),
    ("RBL", re.compile(r"\bRBL\b", re.IGNORECASE)),
    ("Amex", re.compile(r"\bAMEX\b|\bAMERICAN EXPRESS\b", re.IGNORECASE)),
    ("Citi", re.compile(r"\bCITIBANK\b|\bCITI\b", re.IGNORECASE)),
    ("HSBC", re.compile(r"\bHSBC\b", re.IGNORECASE)),
    ("Standard Chartered", re.compile(r"\bSTANDARD CHARTERED\b", re.IGNORECASE)),
)


def find_bank(message: str) -> str | None:
    """Identify the bank whose account or card was charged.

    Earliest mention wins: an ACH debit names the sending bank up front and the
    receiving one later ("debited from HDFC Bank ... Info: ACH D- PUNJAB
    NATIONAL BANK"), and the money left the first.
    """
    best: tuple[int, str] | None = None
    for name, pattern in BANK_PATTERNS:
        if found := pattern.search(message):
            if best is None or found.start() < best[0]:
                best = (found.start(), name)
    return best[1] if best else None


# Merchant name to spend category. Order matters: more specific patterns come
# first, so "AMAZON WEB SERVICES" is a subscription while "AMAZON PAY" is
# shopping, and "TATA 1MG" is healthcare while "TATA POWER" is a utility.
CATEGORY_PATTERNS = (
    (
        "subscriptions",
        re.compile(
            r"\b(RENDER\.COM|AWS|AMAZON WEB SERVICES|GOOGLE CLOUD|GCP|AZURE|GITHUB"
            r"|VERCEL|NETLIFY|CLOUDFLARE|DIGITALOCEAN|HEROKU|OPENAI|ANTHROPIC|CLAUDE"
            r"|NOTION|FIGMA|ADOBE|JETBRAINS|MICROSOFT|GOOGLE ONE|ICLOUD|APPLE"
            r"|SPOTIFY|SLACK|ZOOM|DROPBOX|GODADDY|NAMECHEAP)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "entertainment",
        re.compile(
            r"\b(NETFLIX|PRIME VIDEO|HOTSTAR|DISNEY|JIOCINEMA|SONYLIV|ZEE5|AHA"
            r"|BOOKMYSHOW|PVR|INOX|CINEPOLIS|GAMING|STEAM)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "food_delivery",
        re.compile(
            r"\b(SWIGGY|ZOMATO|EATSURE|BOX8|FAASOS|FRESHMENU|EATFIT|BEHROUZ)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "groceries",
        re.compile(
            r"\b(BIGBASKET|BLINKIT|ZEPTO|INSTAMART|DUNZO|JIOMART|DMART|D MART"
            r"|RELIANCE FRESH|NATURES BASKET|SPENCERS|LICIOUS|COUNTRY DELIGHT"
            r"|MILKBASKET|SUPERMARKET|KIRANA|GROCER)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "dining",
        re.compile(
            r"\b(STARBUCKS|CAFE|COFFEE|CHAAYOS|THIRD WAVE|BLUE TOKAI|RESTAURANT"
            r"|DHABA|BARBEQUE|BARBEQUE NATION|DOMINO|PIZZA|MCDONALD|KFC"
            r"|BURGER KING|SUBWAY|BIRYANI|BAKERY|SWEETS|HOTEL)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "clothing",
        re.compile(
            r"\b(MYNTRA|AJIO|NYKAA FASHION|ZARA|H&M|UNIQLO|LIFESTYLE|PANTALOONS"
            r"|WESTSIDE|SHOPPERS STOP|LEVIS|ALLEN SOLLY|VAN HEUSEN|PETER ENGLAND"
            r"|BEWAKOOF|SNITCH|FABINDIA|BIBA|DECATHLON|NIKE|ADIDAS|PUMA|SKECHERS"
            r"|BATA|METRO SHOES)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "healthcare",
        re.compile(
            r"\b(1MG|TATA 1MG|APOLLO|PHARMEASY|NETMEDS|MEDPLUS|WELLNESS FOREVER"
            r"|PRACTO|CULTFIT|CULT\.FIT|FORTIS|MANIPAL|NARAYANA|MAX HEALTHCARE"
            r"|HOSPITAL|CLINIC|DIAGNOSTIC|PATHOLOGY|LAL PATH|THYROCARE|PHARMACY"
            r"|CHEMIST|MEDICAL|DENTAL|HEALTHCARE)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "travel",
        re.compile(
            r"\b(CLEARTRIP|MAKEMYTRIP|MMT|GOIBIBO|YATRA|EASEMYTRIP|IRCTC|INDIGO"
            r"|AIR INDIA|VISTARA|AKASA|SPICEJET|EMIRATES|QATAR AIRWAYS|LUFTHANSA"
            r"|OYO|AIRBNB|BOOKING\.COM|AGODA|TREEBO|FABHOTELS|REDBUS|ABHIBUS"
            r"|TRAVEL|AIRLINES|AIRPORT)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "transport",
        re.compile(
            r"\b(UBER|OLA|RAPIDO|BLUSMART|NAMMA YATRI|DMRC|METRO RAIL|BMTC|BEST"
            r"|FASTAG|PARKING|TOLL)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "fuel",
        re.compile(
            r"\b(INDIAN OIL|IOCL|IOC|HPCL|HP PETROL|BHARAT PETROLEUM|BPCL|SHELL"
            r"|NAYARA|JIO-?BP|PETROL|DIESEL|FUEL|FILLING STATION)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "utilities",
        re.compile(
            r"\b(ELECTRICITY|BESCOM|MSEB|MSEDCL|TNEB|BSES|ADANI ELECTRICITY"
            r"|TATA POWER|TORRENT POWER|INDANE|HP GAS|BHARATGAS|GAIL|GAS AGENCY"
            r"|AIRTEL|JIO|VODAFONE|VI |BSNL|ACT FIBERNET|HATHWAY|EXCITEL"
            r"|BROADBAND|RECHARGE|TATA SKY|TATAPLAY|DISH TV|D2H|WATER BOARD"
            r"|MUNICIPAL)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "education",
        re.compile(
            r"\b(UDEMY|COURSERA|UPGRAD|BYJU|UNACADEMY|VEDANTU|SCALER|EDUREKA"
            r"|SIMPLILEARN|GREAT LEARNING|SCHOOL|COLLEGE|UNIVERSITY|TUITION"
            r"|EXAM FEE)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "investment",
        re.compile(
            r"\b(ZERODHA|GROWW|UPSTOX|ANGEL ONE|ANGELONE|KUVERA|SMALLCASE"
            r"|INDMONEY|COIN|MUTUAL FUND|MF |SIP |APY|NPS|PPF|ELSS|SUKANYA"
            r"|RECURRING DEPOSIT|FIXED DEPOSIT)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "insurance",
        re.compile(
            r"\b(LIC|HDFC LIFE|ICICI PRU|SBI LIFE|MAX LIFE|TATA AIA|BAJAJ ALLIANZ"
            r"|POLICYBAZAAR|STAR HEALTH|NIVA BUPA|CARE HEALTH|ACKO|DIGIT"
            r"|INSURANCE|POLICY PREMIUM)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "shopping",
        re.compile(
            r"\b(AMAZON|FLIPKART|MEESHO|SNAPDEAL|TATA CLIQ|TATACLIQ|NYKAA|PUROLATOR"
            r"|RELIANCE DIGITAL|CROMA|VIJAY SALES|IKEA|PEPPERFRY|URBAN LADDER"
            r"|FIRSTCRY|LENSKART|TITAN|TANISHQ|CARATLANE|STORE|MART|RETAIL)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "bank_charges",
        re.compile(
            r"\b(SMS CHARGE|CHARGE|CHARGES|\bFEE\b|FEES|GST|ANNUAL FEE|AMC"
            r"|PENALTY|MIN BAL|LATE PAYMENT|INTEREST)\b",
            re.IGNORECASE,
        ),
    ),
)

# When the merchant says nothing, how the money moved still does.
CATEGORY_BY_INSTRUMENT = {
    "ach": "transfer",
    "neft": "transfer",
    "imps": "transfer",
    "atm": "cash",
    "account": "bank_charges",
}

# The one list of spend categories, in the order a picker should show them.
# Parsing fills in whichever it can recognise; a client may assign any of them.
CATEGORIES = (
    "food_delivery",
    "dining",
    "groceries",
    "shopping",
    "clothing",
    "travel",
    "transport",
    "fuel",
    "healthcare",
    "fitness",
    "utilities",
    "rent",
    "home",
    "entertainment",
    "subscriptions",
    "education",
    "investment",
    "insurance",
    "pets",
    "gifts",
    "charity",
    "personal",
    "transfer",
    "cash",
    "bank_charges",
    "other",
)

# Guard against drift: a category the parser assigns but PATCH would refuse is an
# awkward inconsistency to discover in production, so fail at import instead.
_parsed_categories = {name for name, _ in CATEGORY_PATTERNS} | set(
    CATEGORY_BY_INSTRUMENT.values()
)
if not _parsed_categories <= set(CATEGORIES):
    raise ValueError(
        "parser assigns categories missing from CATEGORIES: "
        + ", ".join(sorted(_parsed_categories - set(CATEGORIES)))
    )


def find_category(merchant: str | None, instrument: str | None) -> str | None:
    """Bucket a transaction by what it was for.

    Matches the merchant name against known brands, then falls back to the
    instrument. Returns None rather than guessing — a UPI payment to a person,
    or an unknown shop, is genuinely uncategorised.
    """
    if merchant:
        # Bank descriptors use underscores where a name would use spaces.
        text = merchant.replace("_", " ")
        for name, pattern in CATEGORY_PATTERNS:
            if pattern.search(text):
                return name
    return CATEGORY_BY_INSTRUMENT.get(instrument or "")


CREDIT_WORDS = re.compile(
    r"\b(credited|received|refund(?:ed)?|deposit(?:ed)?|reversal|cashback)\b",
    re.IGNORECASE,
)
DEBIT_WORDS = re.compile(
    r"\b(debited|spent|sent|paid|withdrawn|purchase[d]?|txn|transferred)\b",
    re.IGNORECASE,
)

# Transaction plumbing wrapped around the merchant name.
MERCHANT_LEAD_TAG = re.compile(r"^(?:ach\s+[a-z]{1,2}-|neft-|imps-|upi-)\s*", re.IGNORECASE)
MERCHANT_CODE_TAIL = re.compile(r"[-–]\s*[A-Z]*\d{4,}[A-Z0-9]*$")


def valid_merchant(text: str) -> bool:
    """Reject the phone numbers, account refs and boilerplate that surround it."""
    if not 2 <= len(text) <= 60:
        return False
    if "://" in text.lower():
        return False
    letters = sum(character.isalpha() for character in text)
    digits = sum(character.isdigit() for character in text)
    if not letters or digits > letters:
        return False
    return not (ACCOUNT_LIKE.search(text) or MERCHANT_NOISE.search(text))


def clean_merchant(text: str) -> str:
    """Normalise a merchant name so the same shop groups as one.

    Banks vary the casing of the same merchant between messages ("Cleartrip P"
    and "CLEARTRIP P"), which would otherwise split its total in two. Almost all
    of them shout, so upper case is the honest canonical form.
    """
    text = " ".join(text.split()).strip(" -_")
    text = MERCHANT_LEAD_TAG.sub("", text)
    return MERCHANT_CODE_TAIL.sub("", text).strip(" -_").upper()


def find_merchant(message: str) -> str | None:
    for pattern in MERCHANT_PATTERNS:
        for match in pattern.finditer(message):
            candidate = clean_merchant(match.group("merchant"))
            if valid_merchant(candidate):
                return candidate

    # Axis-style layout: the line directly above "Avl Limit: ...".
    lines = [line.strip() for line in message.splitlines()]
    for index, line in enumerate(lines):
        if index and LIMIT_LINE.match(line):
            candidate = clean_merchant(lines[index - 1])
            if valid_merchant(candidate) and not candidate[0].isdigit():
                return candidate
    return None


class Spend(BaseModel):
    amount: Decimal | None = None
    merchant: str | None = None
    currency: str | None = None
    # "debit", "credit", or None when the wording is unrecognised. Money coming
    # in must not be summed as spending.
    direction: str | None = None
    # How the money moved: credit_card, upi, ach, standing_instruction, ...
    instrument: str | None = None
    # Which bank's account or card was charged.
    bank: str | None = None
    # What the money was for: food_delivery, shopping, travel, ...
    category: str | None = None


def parse_spend(message: str) -> Spend:
    """Extract amount, currency, merchant, direction, instrument, bank, category.

    Every field is best-effort: formats vary by bank and an unrecognised message
    yields nulls rather than an error, leaving `message` as the source of truth.
    """
    amount: Decimal | None = None
    currency: str | None = None

    for match in AMOUNT_RE.finditer(message):
        if BALANCE_LEAD_IN.search(message[max(0, match.start() - 16) : match.start()]):
            continue
        try:
            amount = Decimal(match.group("amt").replace(",", ""))
        except InvalidOperation:
            continue
        currency = CURRENCY_BY_TOKEN.get(match.group("cur").lower().rstrip("."))
        break

    # Credit wins ties: "credited" is the less ambiguous signal, and
    # miscounting income as spending is the more damaging error.
    direction = None
    if CREDIT_WORDS.search(message):
        direction = "credit"
    elif DEBIT_WORDS.search(message):
        direction = "debit"

    merchant = find_merchant(message)
    instrument = find_instrument(message)

    return Spend(
        amount=amount,
        merchant=merchant,
        currency=currency,
        direction=direction,
        instrument=instrument,
        bank=find_bank(message),
        category=find_category(merchant, instrument),
    )


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


def init_db() -> None:
    """Create the table and add later columns, idempotently."""
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
        for column, ddl in (
            ("amount", "ALTER TABLE entries ADD COLUMN IF NOT EXISTS amount NUMERIC(12,2)"),
            ("merchant", "ALTER TABLE entries ADD COLUMN IF NOT EXISTS merchant TEXT"),
            ("currency", "ALTER TABLE entries ADD COLUMN IF NOT EXISTS currency TEXT"),
            ("direction", "ALTER TABLE entries ADD COLUMN IF NOT EXISTS direction TEXT"),
            ("instrument", "ALTER TABLE entries ADD COLUMN IF NOT EXISTS instrument TEXT"),
            ("bank", "ALTER TABLE entries ADD COLUMN IF NOT EXISTS bank TEXT"),
            ("category", "ALTER TABLE entries ADD COLUMN IF NOT EXISTS category TEXT"),
            (
                "category_source",
                "ALTER TABLE entries ADD COLUMN IF NOT EXISTS category_source TEXT"
                " DEFAULT 'auto'",
            ),
        ):
            del column  # named only for readability
            conn.execute(ddl)
        conn.execute("CREATE INDEX IF NOT EXISTS entries_date_idx ON entries (date)")


def backfill_spend() -> int:
    """Populate the parsed columns for rows stored before they existed."""
    with pool.connection() as conn:
        pending = conn.execute(
            "SELECT id, message FROM entries WHERE amount IS NULL AND merchant IS NULL"
        ).fetchall()
        updated = 0
        for entry_id, message in pending:
            spend = parse_spend(message)
            if spend.amount is None and spend.merchant is None:
                continue
            conn.execute(
                "UPDATE entries SET amount = %s, merchant = %s, currency = %s,"
                " direction = %s, instrument = %s, bank = %s,"
                # A manually assigned category outranks anything re-parsed.
                " category = CASE WHEN category_source = 'manual' THEN category"
                " ELSE %s END"
                " WHERE id = %s",
                (
                    spend.amount,
                    spend.merchant,
                    spend.currency,
                    spend.direction,
                    spend.instrument,
                    spend.bank,
                    spend.category,
                    entry_id,
                ),
            )
            updated += 1
    if pending:
        log.info("backfill: parsed %d of %d unparsed rows", updated, len(pending))
    return updated


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open(wait=True, timeout=30)
    init_db()
    backfill_spend()
    yield
    pool.close()


app = FastAPI(title="Expense Tracker API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key"],
    # No cookies are used, and credentialed requests cannot combine with "*".
    allow_credentials=False,
)


@app.get("/ping")
def ping() -> dict:
    """Wake the container without touching the database.

    Free hosting suspends the service when idle; a request to this endpoint is
    held open while it boots, so a client that pings first meets a warm server
    on its next call. Deliberately does no query, leaving the database
    suspended until an actual entry arrives.
    """
    return {"status": "ok"}


# --------------------------------------------------------------------------
# Dates as the iPhone writes them
# --------------------------------------------------------------------------

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
UNICODE_SPACES = (" ", " ", " ", " ")
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


class Entry(BaseModel):
    id: int
    message: str
    date: date
    # Null whenever the SMS did not match a known format; `message` still holds
    # the original text so a client can fall back to showing it raw.
    amount: float | None = None
    merchant: str | None = None
    currency: str | None = None
    direction: str | None = None
    instrument: str | None = None
    bank: str | None = None
    category: str | None = None
    # "auto" when parsed, "manual" once a client has corrected it.
    category_source: str | None = None


# --------------------------------------------------------------------------
# Request handling
# --------------------------------------------------------------------------


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

    spend = parse_spend(entry.message)
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO entries"
            " (message, date, amount, merchant, currency, direction, instrument,"
            " bank, category)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                entry.message,
                entry.date,
                spend.amount,
                spend.merchant,
                spend.currency,
                spend.direction,
                spend.instrument,
                spend.bank,
                spend.category,
            ),
        ).fetchone()
    return Entry(
        id=row[0],
        message=entry.message,
        date=entry.date,
        amount=spend.amount,
        merchant=spend.merchant,
        currency=spend.currency,
        direction=spend.direction,
        instrument=spend.instrument,
        bank=spend.bank,
        category=spend.category,
        category_source="auto",
    )


class CategoryUpdate(BaseModel):
    # None clears the category, putting the row back in the "needs a decision"
    # bucket. Any other value must be one from CATEGORIES.
    category: str | None = None

    @field_validator("category")
    @classmethod
    def known_category(cls, value):
        if value is None:
            return None
        cleaned = value.strip().lower().replace(" ", "_").replace("-", "_")
        if cleaned not in CATEGORIES:
            raise ValueError(
                f"unknown category {value!r}; expected one of " + ", ".join(CATEGORIES)
            )
        return cleaned


@app.get("/categories", dependencies=[Depends(require_api_key)])
def list_categories() -> dict:
    """The category values a client may assign, in picker order."""
    return {"categories": list(CATEGORIES)}


@app.patch(
    "/entries/{entry_id}",
    response_model=Entry,
    dependencies=[Depends(require_api_key)],
)
def update_category(entry_id: int, update: CategoryUpdate) -> Entry:
    """Correct or fill in one entry's category.

    Marks the row `category_source='manual'` so re-parsing never overwrites the
    decision a human made.
    """
    source = "auto" if update.category is None else "manual"
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            "UPDATE entries SET category = %s, category_source = %s WHERE id = %s"
            " RETURNING id, message, date, amount, merchant, currency, direction,"
            " instrument, bank, category, category_source",
            (update.category, source, entry_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No entry with id {entry_id}")
    return Entry(**row)


@app.delete(
    "/entries/{entry_id}",
    response_model=Entry,
    dependencies=[Depends(require_api_key)],
)
def delete_entry(entry_id: int) -> Entry:
    """Remove one entry permanently.

    Returns the deleted row rather than an empty body: `message` and `date` are
    all a client needs to re-POST it, so an undo is possible right after the
    fact. There is no soft delete, so once the response is gone the raw SMS is
    unrecoverable.
    """
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            "DELETE FROM entries WHERE id = %s"
            " RETURNING id, message, date, amount, merchant, currency, direction,"
            " instrument, bank, category, category_source",
            (entry_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No entry with id {entry_id}")
    log.info("deleted entry id=%s", entry_id)
    return Entry(**row)


@app.get(
    "/entries",
    response_model=list[Entry],
    dependencies=[Depends(require_api_key)],
)
def list_entries(
    since: date | None = Query(None, description="Only entries on or after this date"),
    until: date | None = Query(None, description="Only entries on or before this date"),
    limit: int | None = Query(None, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    direction: str | None = Query(
        None, pattern="^(debit|credit)$", description="Spending is direction=debit"
    ),
    instrument: str | None = Query(None, description="e.g. credit_card, upi, ach"),
    bank: str | None = Query(None, description="e.g. HDFC, Axis, ICICI"),
    category: str | None = Query(None, description="e.g. food_delivery, shopping"),
) -> list[Entry]:
    where: list[str] = []
    params: list[object] = []
    if since is not None:
        where.append("date >= %s")
        params.append(since)
    if until is not None:
        where.append("date <= %s")
        params.append(until)
    if direction is not None:
        where.append("direction = %s")
        params.append(direction)
    if instrument is not None:
        where.append("instrument = %s")
        params.append(instrument)
    if bank is not None:
        where.append("bank = %s")
        params.append(bank)
    if category is not None:
        where.append("category = %s")
        params.append(category)

    sort = "DESC" if order == "desc" else "ASC"
    sql = (
        "SELECT id, message, date, amount, merchant, currency, direction,"
        " instrument, bank, category, category_source FROM entries"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY date {sort}, id {sort}"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    if offset:
        sql += " OFFSET %s"
        params.append(offset)

    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(sql, params).fetchall()
    return [Entry(**row) for row in rows]
