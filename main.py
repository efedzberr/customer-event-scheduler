"""
Customer Event Scheduler - Backend API (Railway)

Two endpoints:
- POST /generate-token : Make calls this to get a signed JWT for the booking link.
- GET  /availability   : Bolt frontend calls this with the JWT to fetch free slots.

Environment variables (required):
  JWT_SECRET        - secret for signing/verifying app JWTs (min 32 chars random)
  SF_CLIENT_ID      - Consumer Key from Salesforce External Client App
  SF_USERNAME       - Salesforce user (with sandbox suffix in sandbox)
  SF_LOGIN_URL      - https://test.salesforce.com (sandbox) or https://login.salesforce.com (prod)
  SF_PRIVATE_KEY    - RSA private key (multi-line, including BEGIN/END lines)
  MAKE_API_KEY      - shared secret between Make and this API (random string)
  APP_BASE_URL      - base URL of the Bolt frontend (e.g. https://your-app.bolt.host)
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
    proposedStart: str  # ISO 8601 with timezone, e.g. "2026-05-12T15:00:00-06:00"


class GenerateTokenResponse(BaseModel):
    token: str
    link: str
    jti: str


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
    link = f"{APP_BASE_URL}/agendar?token={token}"

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
    """
    Returns 1-hour slots between work hours, weekdays only,
    from `now` to +DAYS_AHEAD days, that don't conflict with `events`.
    """
    days_output = []

    for d in range(DAYS_AHEAD):
        day = (now + timedelta(days=d)).date()

        # Skip weekends (5=Saturday, 6=Sunday)
        if day.weekday() >= 5:
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

            # Skip past slots
            if slot_start < now:
                continue

            # Check conflicts with existing events (interval overlap)
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
    # Salesforce returns formats like "2026-05-06T14:30:00.000+0000" or "...Z"
    if dt_str.endswith("+0000"):
        dt_str = dt_str.replace("+0000", "+00:00")
    elif dt_str.endswith("Z"):
        dt_str = dt_str.replace("Z", "+00:00")
    dt = datetime.fromisoformat(dt_str)
    return dt.astimezone(TZ)
