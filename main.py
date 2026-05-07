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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import jwt
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

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
WORK_START_HOUR = 8
WORK_START_MINUTE = 30
WORK_END_HOUR = 17  # last slot starts at 16:30, ends at 17:30
DAYS_AHEAD = 7
SLOT_DURATION_HOURS = 1
TOKEN_TTL_DAYS = 7

SF_ID_PATTERN = re.compile(r"^[a-zA-Z0-9]{15,18}$")

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
    link = f"{APP_BASE_URL}/#token={token}"

    return GenerateTokenResponse(token=token, link=link, jti=jti)


@app.get("/availability")
def availability(token: str = Query(...)):
    """Returns the executive's free 1-hour slots for the next 7 days."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "El link ha expirado")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Token inválido")

    executive_id = payload["executiveId"]
    if not SF_ID_PATTERN.match(executive_id):
        raise HTTPException(400, "Invalid executive ID in token")

    access_token, instance_url = authenticate_salesforce()

    now = datetime.now(tz=TZ)
    end = now + timedelta(days=DAYS_AHEAD)
    events = query_executive_events(access_token, instance_url, executive_id, now, end)

    days = calculate_free_slots(events, now)

    return {
        "executive": {"id": payload["executiveId"], "name": payload["executiveName"]},
        "client": {"id": payload["clientId"], "name": payload["clientName"]},
        "proposedStart": payload["proposedStart"],
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

    selected_end = selected_dt + timedelta(hours=SLOT_DURATION_HOURS)

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
    """Authenticates to Salesforce via JWT Bearer Flow. Returns (access_token, instance_url)."""
    now = datetime.now(tz=ZoneInfo("UTC"))
    claims = {
        "iss": SF_CLIENT_ID,
        "sub": SF_USERNAME,
        "aud": SF_LOGIN_URL,
        "exp": int((now + timedelta(minutes=3)).timestamp()),
    }

    assertion = jwt.encode(claims, SF_PRIVATE_KEY, algorithm="RS256")

    response = httpx.post(
        f"{SF_LOGIN_URL}/services/oauth2/token",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
        timeout=10.0,
    )

    if response.status_code != 200:
        raise HTTPException(502, f"Salesforce auth failed: {response.text}")

    data = response.json()
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
        raise HTTPException(502, f"Salesforce query failed: {response.text}")

    return response.json()["records"]


# === Slot calculation ===

def calculate_free_slots(events, now):
    """1-hour slots between work hours, weekdays only, that don't conflict with `events`."""
    days_output = []

    for d in range(DAYS_AHEAD):
        day = (now + timedelta(days=d)).date()

        if day.weekday() >= 5:  # Saturday=5, Sunday=6
            continue

        slots = []
        slot_count = WORK_END_HOUR - WORK_START_HOUR  # 9 slots: 8:30 .. 16:30

        for i in range(slot_count):
            slot_start = datetime(
                year=day.year,
                month=day.month,
                day=day.day,
                hour=WORK_START_HOUR + i,
                minute=WORK_START_MINUTE,
                tzinfo=TZ,
            )
            slot_end = slot_start + timedelta(hours=SLOT_DURATION_HOURS)

            if slot_start < now:
                continue

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

        if slots:
            days_output.append({
                "date": day.isoformat(),
                "slots": slots,
            })

    return days_output


def parse_sf_datetime(dt_str):
    """Parses a Salesforce datetime string into a TZ-aware datetime in our TZ."""
    if dt_str.endswith("+0000"):
        dt_str = dt_str.replace("+0000", "+00:00")
    elif dt_str.endswith("Z"):
        dt_str = dt_str.replace("Z", "+00:00")
    dt = datetime.fromisoformat(dt_str)
    return dt.astimezone(TZ)
