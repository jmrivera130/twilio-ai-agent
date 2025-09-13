# app.py — FastAPI + Twilio Conversation Relay + OpenAI (fallback)
# Local bookings (JSONL) + CSV reports + ICS downloads + optional Google Calendar
# No Cal.com. Timezone-aware and low-latency.
# Adds: name/address capture, unified CSV, and Do-Not-Contact (opt-out) flow.

import os
import re
import json
import uuid
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.responses import PlainTextResponse, JSONResponse
from openai import OpenAI

# ---------- required env ----------
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
RELAY_WSS_URL  = os.environ["RELAY_WSS_URL"]   # wss://<your-app>.onrender.com/relay

# Business timezone (for parsing & storage)
BUSINESS_TZ = os.environ.get("TIMEZONE", "America/Los_Angeles")
TZ = ZoneInfo(BUSINESS_TZ)

# Storage dirs
BOOK_DIR   = Path(os.environ.get("BOOK_DIR", "/tmp/appointments"))
REPORT_DIR = Path(os.environ.get("REPORT_DIR", "/tmp/reports"))
ICS_DIR    = Path(os.environ.get("ICS_DIR", "/tmp/ics"))
BOOK_DIR.mkdir(parents=True, exist_ok=True)
REPORT_DIR.mkdir(parents=True, exist_ok=True)
ICS_DIR.mkdir(parents=True, exist_ok=True)

client = OpenAI(api_key=OPENAI_API_KEY)
app = FastAPI()

SYSTEM_PROMPT = (
    "You are Chloe from Foreclosure Relief Group. Be warm, concise, and clear. "
    "Prefer 1–3 short sentences. Avoid filler. Offer more detail only if asked. "
    "If the caller asks for help, offer to schedule a consultation."
)

# ---------- utils ----------
async def send_text(ws: WebSocket, text: str):
    await ws.send_json({"type": "text", "token": text, "last": True})

def _day_path(d: date) -> Path:
    return BOOK_DIR / f"{d.isoformat()}.jsonl"

def _ics_dt(dt: datetime) -> str:
    u = dt.astimezone(ZoneInfo("UTC"))
    return u.strftime("%Y%m%dT%H%M%SZ")

def make_ics(uid: str, start_dt: datetime, end_dt: datetime, summary: str, description: str) -> str:
    nowz = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//FRG//Chloe//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{nowz}",
        f"DTSTART:{_ics_dt(start_dt)}",
        f"DTEND:{_ics_dt(end_dt)}",
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{description}",
        "END:VEVENT",
        "END:VCALENDAR",
        ""
    ]
    return "\r\n".join(lines)

def _write_jsonl_for_day(day: date, rec: dict):
    p = _day_path(day)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def save_booking(start_dt: datetime, caller_number: str | None,
                 name: str | None, address: str | None,
                 note: str = "Consultation", duration_min: int = 30,
                 gcal_link: str | None = None):
    end_dt = start_dt + timedelta(minutes=duration_min)
    rec = {
        "type": "booking",
        "id": uuid.uuid4().hex[:12],
        "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "caller": caller_number,
        "name": (name or "").strip(),
        "address": (address or "").strip(),
        "note": note,
        "calendar_link": ""
    }
    # Store under the APPOINTMENT DATE so /reports/YYYY-MM-DD shows that day’s bookings
    
    # also write ICS for download
    ics_text = make_ics(rec["id"], start_dt, end_dt,
                        "Foreclosure Relief Consultation",
                        f"Caller: {caller_number or 'unknown'}; Name: {rec['name']}; Address: {rec['address']}")
    (ICS_DIR / f"{rec['id']}.ics").write_text(ics_text, encoding="utf-8")
    # Set calendar link preference (Google link else ICS download)
    rec["calendar_link"] = gcal_link or f"/ics/{rec['id']}.ics"
    _write_jsonl_for_day(start_dt.date(), rec)
    return rec

def save_optout(caller_number: str | None, name: str | None, address: str | None, note: str = "DNC request"):
    now_local = datetime.now(TZ)
    rec = {
        "type": "optout",
        "id": uuid.uuid4().hex[:12],
        "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        "start": "",
        "end": "",
        "caller": caller_number,
        "name": (name or "").strip(),
        "address": (address or "").strip(),
        "note": note,
        "calendar_link": ""
    }
    # Store under TODAY so today's report contains DNCs from today
    _write_jsonl_for_day(now_local.date(), rec)
    return rec

def render_report_csv(day: date) -> str:
    # Unified header for both booking & optout records
    header = ["id","type","created_at","start","end","caller","name","address","note","calendar_link"]
    p = _day_path(day)
    rows = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                j = json.loads(line)
                rows.append([
                    j.get("id",""),
                    j.get("type",""),
                    j.get("created_at",""),
                    j.get("start",""),
                    j.get("end",""),
                    j.get("caller","") or "",
                    j.get("name","") or "",
                    j.get("address","") or "",
                    j.get("note","") or "",
                    j.get("calendar_link","") or "",
                ])
            except Exception:
                continue
    out = [",".join(header)]
    for r in rows:
        safe = ['"{}"'.format(str(x).replace('"','""')) for x in r]
        out.append(",".join(safe))
    return "\n".join(out)

# ---------- optional Google Calendar (service account) ----------
# Add GOOGLE_SERVICE_ACCOUNT_JSON (full JSON string or path) and GOOGLE_CALENDAR_ID (e.g., your@gmail.com)
try:
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request as GoogleRequest
    HAVE_GCAL = True
except Exception:
    service_account = None
    GoogleRequest = None
    HAVE_GCAL = False

async def gcal_create_event(summary: str, start_dt: datetime, end_dt: datetime, description: str | None = None):
    if not HAVE_GCAL:
        return {"ok": False, "error": "google-auth not installed"}
    cal_id = os.environ.get("GOOGLE_CALENDAR_ID")
    if not cal_id:
        return {"ok": False, "error": "GOOGLE_CALENDAR_ID not set"}
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        return {"ok": False, "error": "GOOGLE_SERVICE_ACCOUNT_JSON not set"}
    try:
        # Load JSON (string or path)
        if raw.strip().startswith("{"):
            info = json.loads(raw)
        else:
            info = json.loads(Path(raw).read_text(encoding="utf-8"))
        scopes = ["https://www.googleapis.com/auth/calendar"]
        cred = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        cred.refresh(GoogleRequest())

        import httpx
        url = f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events"
        evt = {
            "summary": summary,
            "description": description or "",
            "start": {"dateTime": start_dt.isoformat(), "timeZone": BUSINESS_TZ},
            "end":   {"dateTime": end_dt.isoformat(),   "timeZone": BUSINESS_TZ},
        }
        headers = {"Authorization": f"Bearer {cred.token}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=20) as x:
            r = await x.post(url, headers=headers, json=evt)
        if r.status_code in (200, 201):
            j = r.json()
            return {"ok": True, "id": j.get("id"), "htmlLink": j.get("htmlLink")}
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": repr(e)}

# ---------- lightweight natural language date/time parsing ----------
WEEKDAYS = {"monday":0,"tuesday":1,"wednesday":2,"thursday":3,"friday":4,"saturday":5,"sunday":6}
MONTHS = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,"july":7,"august":8,"september":9,"october":10,"november":11,"december":12}

def _next_weekday(now_dt: datetime, target: int) -> date:
    days_ahead = (target - now_dt.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return (now_dt + timedelta(days=days_ahead)).date()

def parse_date_phrase(text: str, now_dt: datetime) -> date | None:
    s = (text or "").lower()

    # in N days
    m = re.search(r"\bin\s+(\d+)\s+day[s]?\b", s)
    if m:
        return (now_dt + timedelta(days=int(m.group(1)))).date()

    # today / tomorrow
    if re.search(r"\btoday\b", s):
        return now_dt.date()
    if re.search(r"\btomorrow\b", s):
        return (now_dt + timedelta(days=1)).date()

    # weekday name
    for name, idx in WEEKDAYS.items():
        if re.search(rf"\b{name}\b", s):
            return _next_weekday(now_dt, idx)

    # month name + day (e.g., September 15 / Sep 15)
    m = re.search(r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?)\s+(\d{1,2})\b", s)
    if m:
        month_name = m.group(1)
        mon = None
        for full, num in MONTHS.items():
            if full.startswith(month_name[:3]):
                mon = num
                break
        day = int(m.group(2))
        year = now_dt.year
        try:
            d = date(year, mon, day)
            if d < now_dt.date():
                d = date(year + 1, mon, day)
            return d
        except Exception:
            pass

    # numeric m/d or m-d
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})\b", s)
    if m:
        mon, day = int(m.group(1)), int(m.group(2))
        year = now_dt.year
        try:
            d = date(year, mon, day)
            if d < now_dt.date():
                d = date(year + 1, mon, day)
            return d
        except Exception:
            pass

    # ISO yyyy-mm-dd
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            pass

    return None

def parse_time_phrase(text: str) -> time | None:
    s = (text or "").lower().strip()
    s2 = s.replace(" ", "")
    # keywords
    if "noon" in s:
        return time(12, 0)
    if "midnight" in s:
        return time(0, 0)
    # forms like "at 12", "12", "12pm", "12:30pm", "12:30"
    m = re.search(r"\b(?:at\s*)?(\d{1,2})(?::(\d{2}))?(a\.?m\.?|p\.?m\.?)?\b", s)
    if not m:
        # also allow glued forms when spaces are removed (e.g., "at1pm")
        m = re.search(r"(?:^|[^0-9])(\d{1,2})(?::(\d{2}))?(a\.?m\.?|p\.?m\.?)?(?:$|[^0-9])", s2)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ap = (m.group(3) or "").replace(".", "")
    if ap == "pm" and hour != 12:
        hour += 12
    if ap == "am" and hour == 12:
        hour = 0
    if not ap and hour <= 7:
        # Bare "6" likely evening
        hour += 12
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)

def extract_datetime(text: str):
    now_dt = datetime.now(TZ)
    d = parse_date_phrase(text, now_dt)
    t = parse_time_phrase(text)
    if d and t:
        return d, t, datetime.combine(d, t, tzinfo=TZ)
    return d, t, None
# ---------- helpers: booking & opt-out detection ----------
BOOKING_KEYWORDS_RE = re.compile(r"\b(book|appointment|schedule|set up|meeting|consult)\b", re.I)
YES_RE = re.compile(r"\b(yes|yeah|yep|correct|confirmed|that works|sounds good|ok|okay)\b", re.I)
SCHEDULING_HINT_RE = re.compile(r"\b(schedule|book|appointment|set up|consult)\b", re.I)

def maybe_extract_name(text: str) -> str | None:
    m = re.search(r"\b(my name is|this is)\s+([A-Za-z][A-Za-z\.\-'\s]{1,60})\b", text, re.I)
    if m:
        return m.group(2).strip(" .,'-")
    return None

def maybe_extract_address(text: str) -> str | None:
    m = re.search(r"\b(?:address|property address|the address)\s*(?:is|:)?\s*(.+)", text, re.I)
    if m:
        addr = m.group(1).strip()
        if len(addr) >= 4:
            return addr
    m2 = re.search(r"\b\d{2,6}\s+[A-Za-z0-9][A-Za-z0-9\s\.\-']{3,}\b", text)
    if m2:
        return m2.group(0).strip()
    return None

def maybe_extract_phone(text: str) -> str | None:
    # Pull out a plausible phone number from free text
    digits = re.sub(r"\D+", "", text or "")
    if 10 <= len(digits) <= 15:
        # normalize US 10-digit to +1XXXXXXXXXX if likely
        if len(digits) == 10:
            return "+1" + digits
        if digits.startswith("1") and len(digits) == 11:
            return "+" + digits
        if digits.startswith("+"):
            return digits
        return "+" + digits
    return None

def _tz_label(dt: datetime) -> str:
    # Prefer friendly region for America/Los_Angeles
    try:
        if "Los_Angeles" in BUSINESS_TZ:
            return "Pacific"
        # fallback to zone abbreviation like PST/PDT
        return dt.tzname() or BUSINESS_TZ
    except Exception:
        return BUSINESS_TZ

def when_phrase(dt: datetime) -> str:
    return f"{dt.strftime('%A, %B %d, %I:%M %p')} {_tz_label(dt)}"


# ---------- HTTP: Twilio hits /voice ----------
@app.post("/voice")
async def voice(_: Request):
    # Tell Twilio to open a WebSocket to our /relay endpoint and use Amazon Joanna
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <ConversationRelay
      url="{RELAY_WSS_URL}"
      ttsProvider="Amazon"
      voice="Joanna-Neural"
      interruptible="any"
      reportInputDuringAgentSpeech="speech"
      welcomeGreeting="Hi, I’m Chloe. How can I help?"
    />
  </Connect>
</Response>"""
    return PlainTextResponse(twiml, media_type="text/xml")

# optional sanity endpoints
@app.get("/")
async def index():
    return PlainTextResponse("OK")

# Health alias + HEAD + favicon to quiet logs
@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})

@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True})

@app.head("/")
async def head_root():
    return Response(status_code=200)

@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)

# ICS download
@app.get("/ics/{bid}.ics")
async def get_ics(bid: str):
    p = ICS_DIR / f"{bid}.ics"
    if p.exists():
        return PlainTextResponse(p.read_text(encoding="utf-8"), media_type="text/calendar; charset=utf-8")
    return Response(status_code=404)

# Reports (CSV)
@app.get("/reports/today")
async def report_today():
    d = datetime.now(TZ).date()
    csv_text = render_report_csv(d)
    fname = f"appointments-{d.isoformat()}.csv"
    return PlainTextResponse(csv_text, media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

@app.get("/reports/{day}")
async def report_day(day: str):
    try:
        d = datetime.fromisoformat(day).date()
    except Exception:
        return JSONResponse({"error":"bad date"}, status_code=400)
    csv_text = render_report_csv(d)
    fname = f"appointments-{d.isoformat()}.csv"
    return PlainTextResponse(csv_text, media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

# ---------- WebSocket: Twilio connects here ----------
OPT_OUT_RE = re.compile(r"\b(opt\s*out|do\s*not\s*contact|do\s*not\s*call|don't\s*call|do not call|stop|unsubscribe|remove me|take me off)\b", re.I)

@app.websocket("/relay")
async def relay(ws: WebSocket):
    await ws.accept()
    print("ConversationRelay: connected", flush=True)

    # tiny rolling history for coherence (kept short for latency)
    history: list[dict] = []

    # caller + unified state
    caller_number: str | None = None
    state = {
        "mode": None,          # None | "booking" | "optout"
        "need": None,          # None | "date" | "time" | "name" | "address" | "confirm"
        "hold_date": None,     # date obj
        "hold_time": None,     # time obj
        "hold_dt": None,       # datetime obj
        "hold_name": None,     # str
        "hold_address": None,  # str
        "hold_phone": None     # str (fallback if caller id missing)
    }

    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")

            # Twilio sends this once at call start
            if mtype == "setup":
                caller_number = (msg.get("from") or "").strip() or None
                continue

            # Recognized speech from caller
            if mtype == "prompt":
                user_text = (msg.get("voicePrompt") or "").strip()
                if not user_text:
                    continue
                print("RX:", user_text, flush=True)
                text = user_text

                # prior assistant line (to catch "Yes" after assistant suggested scheduling)
                last_ai = ""
                if history and history[-1].get("role") == "assistant":
                    last_ai = history[-1].get("content", "").lower()

                # Caller asked to clarify the exact date/time
                if re.search(r"\b(what\s+(date|day)\s+is\s+that|which\s+day\s+is\s+that|what\s+date\s+would\s+that\s+be|what\s+day\s+would\s+that\s+be)\b", text, re.I):
                    if state.get("hold_dt"):
                        await send_text(ws, when_phrase(state["hold_dt"]))
                        continue
                    elif state.get("hold_date") and state.get("hold_time"):
                        dt_tmp = datetime.combine(state["hold_date"], state["hold_time"], tzinfo=TZ)
                        await send_text(ws, when_phrase(dt_tmp))
                        continue
                    elif state.get("hold_date"):
                        d = state["hold_date"]
                        phr = f"{d.strftime('%A, %B %d')} {_tz_label(datetime.now(TZ))}"
                        await send_text(ws, phr)
                        continue
                    else:
                        await send_text(ws, "I can confirm the exact date and time once we pick a day and time.")
                        continue

                # -------- global: detect Opt-Out at any time --------
                if OPT_OUT_RE.search(text):
                    state.update({"mode": "optout"})
                    if not state["hold_name"]:
                        state["need"] = "name"
                        await send_text(ws, "Understood. I’ll mark you as do-not-contact. What’s your full name?")
                        continue
                    if not state["hold_address"]:
                        state["need"] = "address"
                        await send_text(ws, "Thanks. What’s the property address to stop contacting?")
                        continue
                    # ensure we have a phone number; ask if caller ID missing
                    if not caller_number and not state["hold_phone"]:
                        state["need"] = "phone"
                        await send_text(ws, "What�?Ts the best number to reach you?")
                    else:
                        state["need"] = "confirm"
                        ph = state["hold_phone"] or caller_number
                        await send_text(ws, f"Confirm do-not-contact for {state['hold_name']} at {state['hold_address']}, phone {ph}. Is that correct?")
                    continue

                # -------- handle ongoing optout flow --------
                if state["mode"] == "optout":
                    if state["need"] == "name":
                        nm = text.strip()
                        if len(nm) < 2:
                            await send_text(ws, "Could you please repeat your full name?")
                            continue
                        state["hold_name"] = nm
                        state["need"] = "address"
                        await send_text(ws, "Thanks. What’s the property address?")
                        continue

                    if state["need"] == "address":
                        addr = text.strip()
                        if len(addr) < 4:
                            await send_text(ws, "Could you repeat the property address?")
                            continue
                        state["hold_address"] = addr
                        # if we don't have a phone number, ask for it
                        if not caller_number and not state["hold_phone"]:
                            state["need"] = "phone"
                            await send_text(ws, "What�?Ts the best number to reach you?")
                        else:
                            state["need"] = "confirm"
                            ph = state["hold_phone"] or caller_number
                            await send_text(ws, f"Confirm do-not-contact for {state['hold_name']} at {state['hold_address']}, phone {ph}. Is that correct?")
                        continue

                    if state["need"] == "phone":
                        ph = maybe_extract_phone(text)
                        if not ph:
                            await send_text(ws, "Sorry, I didn’t catch that. What’s the best callback number?")
                            continue
                        state["hold_phone"] = ph
                        state["need"] = "confirm"
                        await send_text(ws, f"Thanks. Confirm do-not-contact for {state['hold_name']} at {state['hold_address']}, phone {ph}. Is that correct?")
                        continue

                    if state["need"] == "confirm":
                        if re.search(r"\b(yes|yeah|yep|correct|confirmed|that’s right|that is right|ok|okay)\b", text, re.I):
                            ph = state["hold_phone"] or caller_number
                            rec = save_optout(ph, state["hold_name"], state["hold_address"])
                            state = {"mode": None, "need": None, "hold_date": None, "hold_time": None, "hold_dt": None, "hold_name": None, "hold_address": None, "hold_phone": None}
                            await send_text(ws, "All set — I’ve marked you as do-not-contact. Take care.")
                            continue
                        elif re.search(r"\b(no|nope|not|change|different|cancel)\b", text, re.I):
                            state = {"mode": None, "need": None, "hold_date": None, "hold_time": None, "hold_dt": None, "hold_name": None, "hold_address": None, "hold_phone": None}
                            await send_text(ws, "Okay. How else can I help?")
                            continue
                        else:
                            await send_text(ws, "Please say yes or no to confirm.")
                            continue

                # -------- booking: detect intent --------
                d, t, dt = extract_datetime(text)
                booking_keyword = bool(BOOKING_KEYWORDS_RE.search(text))
                yes_after_schedule = bool(YES_RE.search(text) and SCHEDULING_HINT_RE.search(last_ai))
                datetime_implies_booking = bool(d or t)
                if state["mode"] is None and (booking_keyword or yes_after_schedule or datetime_implies_booking):
                    state["mode"] = "booking"
                    # fall through to normal booking flow

                # Passive capture (works in booking/optout): name/address
                if state["mode"] in ("booking", "optout"):
                    if not state["hold_name"]:
                        nm_cap = maybe_extract_name(text)
                        if nm_cap:
                            state["hold_name"] = nm_cap
                    if not state["hold_address"]:
                        addr_cap = maybe_extract_address(text)
                        if addr_cap:
                            state["hold_address"] = addr_cap
                    if not caller_number and not state["hold_phone"]:
                        ph_cap = maybe_extract_phone(text)
                        if ph_cap:
                            state["hold_phone"] = ph_cap

                # -------- booking flow --------
                if state["mode"] == "booking":
                    # If awaiting confirmation, skip prompting here; handle below.
                    if state.get("need") == "confirm":
                        pass
                    else:
                        # Fill what we can from this utterance
                        d, t, dt = extract_datetime(text)
                        if d:
                            state["hold_date"] = d
                        if t:
                            state["hold_time"] = t
                        if state["hold_date"] and state["hold_time"]:
                            state["hold_dt"] = datetime.combine(state["hold_date"], state["hold_time"], tzinfo=TZ)

                    # ask in order: date -> time -> name -> address -> confirm
                    if not state["hold_date"]:
                        state["need"] = "date"
                        await send_text(ws, "What day works for you? (e.g., Tuesday or September 15)")
                        continue
                    if not state["hold_time"]:
                        state["need"] = "time"
                        await send_text(ws, "What time should I book? (e.g., 12 PM)")
                        continue
                    if not state["hold_name"]:
                        state["need"] = "name"
                        when_say = when_phrase(state["hold_dt"])
                        await send_text(ws, f"Great — I have {when_say}. What’s your full name?")
                        continue
                    if not state["hold_address"]:
                        state["need"] = "address"
                        await send_text(ws, "Thanks. What’s the property address?")
                        continue

                    # ensure we have a phone number
                    if not caller_number and not state["hold_phone"]:
                        state["need"] = "phone"
                        await send_text(ws, "What’s the best number to reach you?")
                        continue

                        # confirm all details
                        state["need"] = "confirm"
                        when_say = when_phrase(state["hold_dt"])
                        await send_text(ws, f"Just to confirm: {state['hold_name']} at {state['hold_address']} on {when_say}. Is that correct?")
                        continue

                # -------- respond normally if not in a flow --------
                if state["mode"] is None:
                    try:
                        resp = client.responses.create(
                            model="gpt-4o-mini",
                            input=[
                                {"role": "system", "content": SYSTEM_PROMPT},
                                *history[-6:],  # last 3 turns (user+assistant)
                                {"role": "user", "content": user_text},
                            ],
                            max_output_tokens=180,
                            temperature=0.3,
                        )
                        ai_text = (resp.output_text or "").strip()
                        if not ai_text:
                            ai_text = "Sorry, could you repeat that?"
                    except Exception as e:
                        print("OpenAI error:", repr(e), flush=True)
                        ai_text = "I’m having trouble right now. Please say that again."

                    print("TX:", ai_text, flush=True)
                    history.append({"role": "user", "content": user_text})
                    history.append({"role": "assistant", "content": ai_text})
                    await send_text(ws, ai_text)
                    continue

                # -------- booking confirmation handler --------
                if state["mode"] == "booking" and state["need"] == "confirm":
                    if re.search(r"\b(yes|yeah|yep|correct|confirmed|that works|sounds good|ok|okay)\b", text, re.I):
                        start_dt = state["hold_dt"]
                        phone_used = state["hold_phone"] or caller_number
                        g_link = None
                        # Optional Google Calendar push
                        if HAVE_GCAL and os.environ.get("GOOGLE_CALENDAR_ID") and os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
                            g = await gcal_create_event(
                                "Foreclosure Relief Consultation",
                                start_dt,
                                start_dt + timedelta(minutes=30),
                                description=f"Caller: {phone_used or 'unknown'}; Name: {state['hold_name']}; Address: {state['hold_address']}",
                            )
                            if g.get("ok"):
                                g_link = g.get("htmlLink")
                        rec = save_booking(start_dt, phone_used, state["hold_name"], state["hold_address"], gcal_link=g_link)
                        when_say = when_phrase(start_dt)
                        state = {"mode": None, "need": None, "hold_date": None, "hold_time": None, "hold_dt": None, "hold_name": None, "hold_address": None, "hold_phone": None}
                        if g_link:
                            await send_text(ws, f"All set — booked for {when_say}. I’ve added it to the calendar.")
                        else:
                            await send_text(ws, f"All set — you’re booked for {when_say}.")
                        continue
                    elif re.search(r"\b(no|nope|not|change|different|cancel)\b", text, re.I):
                        state["need"] = "date"
                        await send_text(ws, "No problem. What day and time would you like instead?")
                        continue
                    else:
                        await send_text(ws, "Please say yes or no to confirm.")
                        continue

                # -------- filling fields if we’re mid-flow but not at confirm --------
                if state["mode"] == "booking":
                    if state["need"] == "date":
                        d, _, _ = extract_datetime(text)
                        if d:
                            state["hold_date"] = d
                            if state["hold_time"]:
                                state["hold_dt"] = datetime.combine(d, state["hold_time"], tzinfo=TZ)
                        else:
                            await send_text(ws, "Got it. What day did you have in mind? (e.g., Tuesday or September 15)")
                            continue
                    elif state["need"] == "time":
                        _, t, _ = extract_datetime(text)
                        if t:
                            state["hold_time"] = t
                            if state["hold_date"]:
                                state["hold_dt"] = datetime.combine(state["hold_date"], t, tzinfo=TZ)
                        else:
                            await send_text(ws, "What time should I book? (e.g., 12 PM)")
                            continue
                    elif state["need"] == "name":
                        nm = text.strip()
                        if len(nm) < 2:
                            await send_text(ws, "Could you please repeat your full name?")
                            continue
                        state["hold_name"] = nm
                    elif state["need"] == "address":
                        addr = text.strip()
                        if len(addr) < 4:
                            await send_text(ws, "Could you repeat the property address?")
                            continue
                        state["hold_address"] = addr
                    elif state["need"] == "phone":
                        ph = maybe_extract_phone(text)
                        if not ph:
                            await send_text(ws, "Sorry, I didn’t catch that. What’s the best callback number?")
                            continue
                        state["hold_phone"] = ph

                    # after filling, proceed down the chain again
                    if not state["hold_date"]:
                        await send_text(ws, "What day works for you? (e.g., Tuesday or September 15)")
                        continue
                    if not state["hold_time"]:
                        await send_text(ws, "What time should I book? (e.g., 12 PM)")
                        continue
                    if not state["hold_name"]:
                        when_say = when_phrase(datetime.combine(state["hold_date"], state["hold_time"], tzinfo=TZ))
                        await send_text(ws, f"Great — I have {when_say}. What’s your full name?")
                        continue
                    if not state["hold_address"]:
                        await send_text(ws, "Thanks. What’s the property address?")
                        continue
                    state["hold_dt"] = datetime.combine(state["hold_date"], state["hold_time"], tzinfo=TZ)
                    if not caller_number and not state["hold_phone"]:
                        await send_text(ws, "What’s the best number to reach you?")
                        state["need"] = "phone"
                        continue
                    state["need"] = "confirm"
                    when_say = when_phrase(state["hold_dt"])
                    await send_text(ws, f"Just to confirm: {state['hold_name']} at {state['hold_address']} on {when_say}. Is that correct?")
                    continue

                # fallback safety
                await send_text(ws, "Sorry, could you repeat that?")
                continue

            if mtype == "interrupt":
                print("Interrupted:", msg.get("utteranceUntilInterrupt", ""), flush=True)
                continue

            if mtype == "error":
                print("ConversationRelay error:", msg.get("description"), flush=True)
                continue

            # ignore other frame types
    except WebSocketDisconnect:
        print("ConversationRelay: disconnected", flush=True)
