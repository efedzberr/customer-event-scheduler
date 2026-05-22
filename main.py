"""
Customer Event Scheduler - Backend API (Railway)

Endpoints:
- POST /generate-token : Make calls this to get a signed JWT for the booking link.
- GET  /availability   : Bolt frontend calls this with the JWT to fetch free slots.
- POST /book           : Make calls this when client confirms a slot. Validates JWT,
                         re-checks availability, writes Supabase, creates Event in SF,
                         updates Supabase with eventId.

Environment variables (required):
  JWT_SECRET           - secret for signing/verifying app JWTs (min 32 chars random)
  SF_CLIENT_ID         - Consumer Key from Salesforce External Client App
  SF_USERNAME          - Salesforce user (with sandbox suffix in sandbox)
  SF_LOGIN_URL         - https://test.salesforce.com (sandbox) or https://login.salesforce.com (prod)
  SF_PRIVATE_KEY       - RSA private key (multi-line, including BEGIN/END lines)
  MAKE_API_KEY         - shared secret between Make and this API
  APP_BASE_URL         - base URL of the Bolt frontend (e.g. https://your-app.bolt.host)
  SUPABASE_URL         - Supabase project URL (e.g. https://xxxxx.supabase.co)
  SUPABASE_SERVICE_KEY - Supabase service_role key (NOT the anon key)
"""

import os
import re
import uuid
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import jwt
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# === Logging ===
# Logs go to stdout so they show up in Railway's "Deploy Logs".
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("event-scheduler")

# === Configuration ===

JWT_SECRET = os.environ["JWT_SECRET"]
SF_CLIENT_ID = os.environ["SF_CLIENT_ID"]
SF_USERNAME = os.environ["SF_USERNAME"]
SF_LOGIN_URL = os.environ["SF_LOGIN_URL"]
SF_PRIVATE_KEY = os.environ["SF_PRIVATE_KEY"]
MAKE_API_KEY = os.environ["MAKE_API_KEY"]
APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://your-bolt-app.example.com")
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

# === Business rules ===

TZ = ZoneInfo("America/Mexico_City")
WEEKDAYS_TO_SHOW = 5         # number of business days (Mon-Fri) shown to the client
TOKEN_TTL_DAYS = 7

# Working hours that bound slot generation. Slots are spaced 1 hour apart.
# Whether they start on the hour (8:00, 9:00, ...) or on the half (8:30, 9:30, ...)
# is decided dynamically per-link from the executive's proposedStart minute.
WORK_START_HOUR = 8           # earliest slot can start at 8:00 or 8:30
WORK_END_HOUR = 18            # latest slot must END at or before 18:30
                              # (so latest start is 17:30 if duration is 60 min)
SLOT_DURATION_MINUTES = 60

SF_ID_PATTERN = re.compile(r"^[a-zA-Z0-9]{15,18}$")

# Spanish day/month names — we format dates server-side to avoid timezone
# parsing bugs in the browser (`new Date("2026-05-11")` is parsed as UTC
# midnight and shifts a day backward in negative-UTC timezones).
SPANISH_WEEKDAYS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
SPANISH_MONTHS = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def format_spanish_day(day):
    """Returns 'Lunes, 11 de mayo'."""
    return f"{SPANISH_WEEKDAYS[day.weekday()]}, {day.day} de {SPANISH_MONTHS[day.month - 1]}"


# === Startup diagnostics ===
# Logged once when the app boots so we can confirm in Railway's Deploy Logs
# that the environment variables point at the right Salesforce org. No secret
# values are printed — only safe prefixes/lengths.

def _log_startup_config():
    """Logs a safe summary of the Salesforce-related config at boot time."""
    pk = SF_PRIVATE_KEY or ""
    pk_has_begin = "BEGIN" in pk
    pk_has_end = "END" in pk
    pk_has_real_newlines = "\n" in pk
    pk_has_escaped_newlines = "\\n" in pk
    logger.info("=== STARTUP CONFIG (Salesforce) ===")
    logger.info("SF_LOGIN_URL = %r", SF_LOGIN_URL)
    logger.info("SF_USERNAME  = %r", SF_USERNAME)
    logger.info("SF_CLIENT_ID length = %d, prefix = %r",
                len(SF_CLIENT_ID or ""), (SF_CLIENT_ID or "")[:10])
    logger.info(
        "SF_PRIVATE_KEY length = %d | has BEGIN = %s | has END = %s | "
        "real newlines = %s | escaped \\n = %s",
        len(pk), pk_has_begin, pk_has_end, pk_has_real_newlines, pk_has_escaped_newlines,
    )
    if pk_has_escaped_newlines and not pk_has_real_newlines:
        logger.warning(
            "SF_PRIVATE_KEY appears to contain literal \\n instead of real "
            "line breaks. This will likely break RS256 signing."
        )
    logger.info("===================================")


_log_startup_config()


# === FastAPI app ===

app = FastAPI(title="Customer Event Scheduler API")

# CORS - Bolt frontend will call /availability from the browser
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: restrict to Bolt domain in production
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# === Models ===

class GenerateTokenRequest(BaseModel):
    clientId: str
    executiveId: str
    clientName: str
    executiveName: str
    proposedStart: str  # ISO 8601 with timezone


class GenerateTokenResponse(BaseModel):
    token: str
    link: str
    jti: str


class BookRequest(BaseModel):
    token: str
    selectedDatetime: str  # ISO 8601 with timezone


# === Endpoints ===

@app.get("/")
def health():
    return {"status": "ok", "service": "customer-event-scheduler"}


@app.post("/generate-token", response_model=GenerateTokenResponse)
def generate_token(req: GenerateTokenRequest, authorization: str = Header(None)):
    """Generates a signed JWT for the scheduling link. Called by Make."""
    if authorization != f"Bearer {MAKE_API_KEY}":
        raise HTTPException(401, "Unauthorized")

    if not SF_ID_PATTERN.match(req.clientId):
        raise HTTPException(400, "Invalid clientId format")
    if not SF_ID_PATTERN.match(req.executiveId):
        raise HTTPException(400, "Invalid executiveId format")

    jti = str(uuid.uuid4())
    exp = datetime.now(tz=ZoneInfo("UTC")) + timedelta(days=TOKEN_TTL_DAYS)

    payload = {
        "clientId": req.clientId,
        "executiveId": req.executiveId,
        "clientName": req.clientName,
        "executiveName": req.executiveName,
        "proposedStart": req.proposedStart,
        "jti": jti,
        "exp": int(exp.timestamp()),
    }

    token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
    # Use hash fragment (#token=) instead of query string (?token=) because
    # bolt.host hosting returns a generic JSON error for non-root paths with
    # query strings. Hash fragments are client-side only — the server sees
    # path "/" and serves index.html, then the React app reads the token
    # from window.location.hash.
    link = f"{APP_BASE_URL}/#token={token}"

    return GenerateTokenResponse(token=token, link=link, jti=jti)


@app.get("/availability")
def availability(token: str = Query(...)):
    """Returns the executive's free 1-hour slots for the next 7 weekdays,
    plus the proposedStart's day if it falls beyond that window."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "El link ha expirado")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Token inválido")

    executive_id = payload["executiveId"]
    if not SF_ID_PATTERN.match(executive_id):
        raise HTTPException(400, "Invalid executive ID in token")

    # Reject the token early if it was already used to book a meeting.
    # This prevents the client from seeing the slot picker after they've
    # already confirmed once with this same link.
    if check_token_used(payload["jti"]):
        raise HTTPException(status_code=409, detail="Este link ya fue usado")

    # Parse proposedStart into a TZ-aware datetime. We use parse_sf_datetime
    # because Salesforce serializes DateTime values as e.g. "2026-05-22T17:30:00.000+0000"
    # or "...Z" — both forms are handled by that helper.
    proposed_start_str = payload.get("proposedStart")
    proposed_start_dt = None
    proposed_start_normalized = proposed_start_str  # fallback if parsing fails
    if proposed_start_str:
        try:
            proposed_start_dt = parse_sf_datetime(proposed_start_str)
            # Normalize to Mexico TZ ISO format so the frontend's string
            # comparison against slot.datetime works regardless of the
            # original timezone in the JWT.
            proposed_start_normalized = proposed_start_dt.isoformat()
        except (ValueError, TypeError):
            proposed_start_dt = None

    access_token, instance_url = authenticate_salesforce()

    now = datetime.now(tz=TZ)

    # Build the events query window so it covers all the weekdays we'll show.
    # Worst case: proposed day is a Monday → Mon..Fri = 5 calendar days.
    # Best case: proposed day is a Friday → Fri + Mon..Thu next week = 7 days.
    # We pad with a small buffer to be safe.
    query_start = now
    if proposed_start_dt and proposed_start_dt < now:
        query_start = proposed_start_dt  # defensive, shouldn't happen
    query_end_anchor = proposed_start_dt if proposed_start_dt else now
    query_end = query_end_anchor + timedelta(days=10)

    events = query_executive_events(access_token, instance_url, executive_id, query_start, query_end)

    days = calculate_free_slots(events, now, proposed_start_dt)

    return {
        "executive": {"id": payload["executiveId"], "name": payload["executiveName"]},
        "client": {"id": payload["clientId"], "name": payload["clientName"]},
        "proposedStart": proposed_start_normalized,
        "days": days,
    }


@app.post("/book")
def book(req: BookRequest, authorization: str = Header(None)):
    """
    Books a slot end-to-end. Called by Make.
    Steps:
    1. Validate JWT
    2. Authenticate to Salesforce
    3. Re-check slot is still available (defensive against race conditions)
    4. INSERT into Supabase tokens_usados (idempotency via unique jti)
    5. Create Event in Salesforce
    6. Update Supabase row with eventId
    Returns either {status:"confirmed", eventId, scheduledAt}
    or         {status:"error", code, message}.
    """
    if authorization != f"Bearer {MAKE_API_KEY}":
        raise HTTPException(401, "Unauthorized")

    # 1. Validate JWT
    try:
        payload = jwt.decode(req.token, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return {"status": "error", "code": "TOKEN_EXPIRED", "message": "El link ha expirado"}
    except jwt.InvalidTokenError:
        return {"status": "error", "code": "INVALID_TOKEN", "message": "Token inválido"}

    jti = payload["jti"]
    executive_id = payload["executiveId"]
    client_id = payload["clientId"]
    client_name = payload["clientName"]
    proposed_start = payload["proposedStart"]

    # Parse and validate selected datetime
    try:
        selected_dt = datetime.fromisoformat(req.selectedDatetime)
    except ValueError:
        return {"status": "error", "code": "INVALID_DATETIME", "message": "Formato de fecha inválido"}

    selected_end = selected_dt + timedelta(minutes=SLOT_DURATION_MINUTES)

    # 2. Authenticate to Salesforce
    try:
        access_token, instance_url = authenticate_salesforce()
    except HTTPException:
        return {"status": "error", "code": "INTERNAL", "message": "Error conectando a Salesforce"}

    # 3. Re-check slot availability (small window query around the slot)
    check_start = selected_dt - timedelta(hours=1)
    check_end = selected_end + timedelta(hours=1)
    try:
        existing_events = query_executive_events(
            access_token, instance_url, executive_id, check_start, check_end
        )
    except HTTPException:
        return {"status": "error", "code": "INTERNAL", "message": "Error consultando calendario"}

    for ev in existing_events:
        ev_start = parse_sf_datetime(ev["StartDateTime"])
        ev_end = parse_sf_datetime(ev["EndDateTime"])
        if selected_dt < ev_end and selected_end > ev_start:
            return {"status": "error", "code": "SLOT_TAKEN", "message": "Ese horario ya no está disponible"}

    # 4. Insert into Supabase tokens_usados (idempotency via unique jti)
    try:
        supabase_response = httpx.post(
            f"{SUPABASE_URL}/rest/v1/tokens_usados",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            json={
                "jti": jti,
                "client_id": client_id,
                "executive_id": executive_id,
                "proposed_start": proposed_start,
                "selected_start": req.selectedDatetime,
            },
            timeout=10.0,
        )
    except Exception as e:
        return {"status": "error", "code": "INTERNAL", "message": f"Supabase unreachable: {str(e)[:200]}"}

    if supabase_response.status_code == 409:
        return {"status": "error", "code": "TOKEN_USED", "message": "Este link ya fue usado"}

    if supabase_response.status_code not in (200, 201, 204):
        return {
            "status": "error",
            "code": "INTERNAL",
            "message": f"Supabase: {supabase_response.text[:200]}",
        }

    # 5. Create Event in Salesforce
    start_utc = selected_dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_utc = selected_end.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    event_payload = {
        "OwnerId": executive_id,
        "WhoId": client_id,
        "StartDateTime": start_utc,
        "EndDateTime": end_utc,
        "Subject": f"Junta con {client_name}",
        "Description": "Junta agendada por el cliente vía link de auto-agenda.",
        "IsAllDayEvent": False,
    }

    sf_response = httpx.post(
        f"{instance_url}/services/data/v59.0/sobjects/Event",
        json=event_payload,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        timeout=10.0,
    )

    if sf_response.status_code not in (200, 201):
        # Note: Supabase row remains as "spent" — token won't work again.
        # Acceptable: better to require a new link than risk creating duplicate events.
        logger.error("Event creation failed: status=%s body=%s",
                      sf_response.status_code, sf_response.text[:500])
        return {
            "status": "error",
            "code": "INTERNAL",
            "message": f"Salesforce: {sf_response.text[:200]}",
        }

    event_id = sf_response.json()["id"]

    # 6. Update Supabase row with eventId (best effort; not critical)
    try:
        httpx.patch(
            f"{SUPABASE_URL}/rest/v1/tokens_usados?jti=eq.{jti}",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
            },
            json={"event_id": event_id},
            timeout=10.0,
        )
    except Exception:
        pass  # Event was created successfully; Supabase update is bookkeeping.

    return {
        "status": "confirmed",
        "eventId": event_id,
        "scheduledAt": req.selectedDatetime,
    }


# === Salesforce JWT Bearer Flow ===

def authenticate_salesforce():
    """Authenticates to Salesforce via JWT Bearer Flow. Returns (access_token, instance_url).

    Detailed diagnostics are logged to stdout (Railway "Deploy Logs") so that
    any auth failure shows the exact error returned by Salesforce.
    """
    now = datetime.now(tz=ZoneInfo("UTC"))
    claims = {
        "iss": SF_CLIENT_ID,
        "sub": SF_USERNAME,
        "aud": SF_LOGIN_URL,
        "exp": int((now + timedelta(minutes=3)).timestamp()),
    }

    logger.info(
        "[SF AUTH] starting JWT Bearer flow | aud=%r sub=%r iss_prefix=%r",
        SF_LOGIN_URL, SF_USERNAME, (SF_CLIENT_ID or "")[:10],
    )

    # Build the signed assertion. If the private key is malformed (bad pasting
    # in Railway, wrong format, etc.) this raises before any network call.
    try:
        assertion = jwt.encode(claims, SF_PRIVATE_KEY, algorithm="RS256")
    except Exception as e:
        logger.error(
            "[SF AUTH] could not sign the JWT assertion. This usually means "
            "SF_PRIVATE_KEY is malformed (missing BEGIN/END lines, literal \\n "
            "instead of real line breaks, or not an RSA key). Error: %s",
            repr(e),
        )
        raise HTTPException(502, f"Salesforce auth failed: cannot sign assertion ({e})")

    token_url = f"{SF_LOGIN_URL}/services/oauth2/token"
    try:
        response = httpx.post(
            token_url,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            timeout=10.0,
        )
    except Exception as e:
        # Network-level failure: DNS, TLS, connection refused, timeout, etc.
        logger.error("[SF AUTH] network error calling %s : %s", token_url, repr(e))
        raise HTTPException(502, f"Salesforce auth failed: network error ({e})")

    if response.status_code != 200:
        # This is the important one: Salesforce reached us back but REJECTED
        # the authentication. The body explains exactly why (invalid_grant,
        # invalid_client, etc). We log it in full so it shows up in Railway.
        logger.error(
            "[SF AUTH] REJECTED by Salesforce | status=%s | body=%s",
            response.status_code, response.text,
        )
        logger.error(
            "[SF AUTH] context for the rejection above -> "
            "login_url=%r username=%r client_id_prefix=%r",
            SF_LOGIN_URL, SF_USERNAME, (SF_CLIENT_ID or "")[:10],
        )
        raise HTTPException(502, f"Salesforce auth failed: {response.text}")

    data = response.json()
    logger.info("[SF AUTH] success | instance_url=%s", data.get("instance_url"))
    return data["access_token"], data["instance_url"]


def query_executive_events(access_token, instance_url, executive_id, start_dt, end_dt):
    """Queries the executive's existing Events in the date range via SOQL."""
    start_utc = start_dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_utc = end_dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

    soql = (
        f"SELECT Id, StartDateTime, EndDateTime FROM Event "
        f"WHERE OwnerId = '{executive_id}' "
        f"AND StartDateTime >= {start_utc} "
        f"AND StartDateTime <= {end_utc} "
        f"AND IsDeleted = false"
    )

    response = httpx.get(
        f"{instance_url}/services/data/v59.0/query",
        params={"q": soql},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10.0,
    )

    if response.status_code != 200:
        logger.error(
            "[SF QUERY] failed | status=%s | body=%s",
            response.status_code, response.text,
        )
        raise HTTPException(502, f"Salesforce query failed: {response.text}")

    return response.json()["records"]


# === Slot calculation ===

def calculate_free_slots(events, now, proposed_start_dt=None):
    """
    Generates 1-hour slots for WEEKDAYS_TO_SHOW (5) business days starting
    from `proposed_start_dt`'s date forward. Weekends are skipped: if a Sat/Sun
    falls inside the window, we keep walking forward into the next week until
    we have 5 business days.

    Slot minute alignment is determined by `proposed_start_dt.minute`:
      - if 0  → slots at  8:00, 9:00, 10:00, ... 17:00  (10 slots)
      - if 30 → slots at  8:30, 9:30, 10:30, ... 17:30  (10 slots)
      - other minutes are normalized to the closest of the two.

    Returns only AVAILABLE slots (filters out past slots and conflicting ones).
    Days with zero available slots are still returned (so the day always shows
    up in the UI), but with an empty `slots` array.
    """
    # Decide where to start. If no proposedStart, fall back to "today".
    if proposed_start_dt:
        start_day = proposed_start_dt.date()
        # Determine slot minute from proposedStart. Round to nearest of {0, 30}.
        slot_minute = 0 if proposed_start_dt.minute < 15 else (
            30 if proposed_start_dt.minute < 45 else 0
        )
        # Note: if proposed minute is e.g. 45+, we round up to next hour at :00.
        # Most calls use exact :00 or :30 so this is a defensive fallback.
    else:
        start_day = now.date()
        slot_minute = 30  # default historical behavior

    # If proposedStart's date is in the past relative to "now", clamp to today.
    # The user explicitly said "no hacia atrás" — never go backwards.
    today = now.date()
    if start_day < today:
        start_day = today

    # If start_day lands on a weekend, advance to the next Monday.
    while start_day.weekday() >= 5:
        start_day += timedelta(days=1)

    # Walk forward collecting WEEKDAYS_TO_SHOW business days.
    days_to_consider = []
    current_day = start_day
    while len(days_to_consider) < WEEKDAYS_TO_SHOW:
        if current_day.weekday() < 5:  # Mon=0 .. Fri=4
            days_to_consider.append(current_day)
        current_day += timedelta(days=1)

    # Build the hourly slot list once — same shape for every day.
    slot_hours = list(range(WORK_START_HOUR, WORK_END_HOUR))  # e.g. [8..17]
    # When minute is 30 the last slot starts at 17:30 and ends at 18:30,
    # which is fine since WORK_END_HOUR = 18 means "must end by 18:30".

    days_output = []
    for day in days_to_consider:
        slots = []

        for hour in slot_hours:
            slot_start = datetime(
                year=day.year,
                month=day.month,
                day=day.day,
                hour=hour,
                minute=slot_minute,
                tzinfo=TZ,
            )
            slot_end = slot_start + timedelta(minutes=SLOT_DURATION_MINUTES)

            # Skip past slots.
            if slot_start < now:
                continue

            # Skip conflicting slots.
            conflicts = False
            for ev in events:
                ev_start = parse_sf_datetime(ev["StartDateTime"])
                ev_end = parse_sf_datetime(ev["EndDateTime"])
                if slot_start < ev_end and slot_end > ev_start:
                    conflicts = True
                    break

            if not conflicts:
                slots.append({
                    "time": slot_start.strftime("%H:%M"),
                    "datetime": slot_start.isoformat(),
                })

        # Always include the day in the output, even if no slots — keeps UI
        # consistent so the client sees the date header even if it's full.
        days_output.append({
            "date": day.isoformat(),
            "label": format_spanish_day(day),
            "slots": slots,
        })

    return days_output


def check_token_used(jti):
    """Returns True if the jti is already in tokens_usados (token spent).
    Used by /availability to reject reuse of an already-confirmed link.
    On any Supabase error, returns False (best-effort) — /book has the
    authoritative check via the unique-jti constraint."""
    try:
        response = httpx.get(
            f"{SUPABASE_URL}/rest/v1/tokens_usados",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            },
            params={"jti": f"eq.{jti}", "select": "jti"},
            timeout=5.0,
        )
        if response.status_code == 200:
            return len(response.json()) > 0
    except Exception:
        pass
    return False


def parse_sf_datetime(dt_str):
    """Parses a Salesforce datetime string into a TZ-aware datetime in our TZ."""
    if dt_str.endswith("+0000"):
        dt_str = dt_str.replace("+0000", "+00:00")
    elif dt_str.endswith("Z"):
        dt_str = dt_str.replace("Z", "+00:00")
    dt = datetime.fromisoformat(dt_str)
    return dt.astimezone(TZ)
