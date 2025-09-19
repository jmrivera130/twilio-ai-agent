# app.py — FastAPI + Twilio ConversationRelay + OpenAI (Sept 2025 compatible)
# Local bookings (CSV/JSONL/ICS), opt-out, future slot suggestions.
# RELAY_WSS_URL must be: wss://<your-app>.onrender.com/relay

import os, re, json, uuid
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from openai import OpenAI

# ---------- env ----------
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
RELAY_WSS_URL  = os.environ["RELAY_WSS_URL"]    # e.g., wss://your-app.onrender.com/relay
BUSINESS_TZ    = os.environ.get("TIMEZONE", os.environ.get("CALCOM_TIMEZONE", "America/Los_Angeles"))
TZ = ZoneInfo(BUSINESS_TZ)

# Voice normalization for Twilio + Amazon Polly
TTS_VOICE_ENV = os.environ.get("TTS_VOICE", "Polly.Joanna")
_base = TTS_VOICE_ENV.split(".")[-1]
_base = re.sub(r"(?i)-?neural$", "", _base)
VOICE_OUT = f"Polly.{_base}"

# Storage
BOOK_DIR   = Path(os.environ.get("BOOK_DIR", "/tmp/appointments")); BOOK_DIR.mkdir(parents=True, exist_ok=True)
REPORT_DIR = Path(os.environ.get("REPORT_DIR", "/tmp/reports")); REPORT_DIR.mkdir(parents=True, exist_ok=True)
ICS_DIR    = Path(os.environ.get("ICS_DIR", "/tmp/ics")); ICS_DIR.mkdir(parents=True, exist_ok=True)

client = OpenAI(api_key=OPENAI_API_KEY)
app = FastAPI()

SYSTEM_PROMPT = (
    "You are Chloe from Foreclosure Relief Group. Be warm, concise, and clear. "
    "Prefer 1–3 short sentences. Avoid filler. Offer more detail only if asked. "
    "When scheduling, gather name, the full property address from the notice, and day/time. "
    "Paraphrase knowledge in your own words—no quoting documents."
)

# ---------- helpers ----------
def normalize_phone(s: str | None) -> str | None:
    if not s: return None
    d = re.sub(r"\D+", "", s)
    if len(d) == 11 and d.startswith("1"): d = d[1:]
    if len(d) == 10: return f"+1{d}"
    return f"+{d}" if d else None

def _day_path(day: date) -> Path:
    return REPORT_DIR / f"appointments-{day.isoformat()}.jsonl"

def _write_jsonl_for_day(day: date, rec: dict):
    with _day_path(day).open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def _ics_dt(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")

def make_ics(uid: str, start_dt: datetime, end_dt: datetime, summary: str, description: str) -> str:
    nowz = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR","VERSION:2.0","PRODID:-//FRG//Chloe//EN","CALSCALE:GREGORIAN","METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}", f"DTSTAMP:{nowz}",
        f"DTSTART;TZID={TZ.key}:{_ics_dt(start_dt)}",
        f"DTEND;TZID={TZ.key}:{_ics_dt(end_dt)}",
        f"SUMMARY:{summary}", f"DESCRIPTION:{description}",
        "END:VEVENT","END:VCALENDAR",""
    ]
    return "\n".join(lines)

def render_report_csv(day: date) -> str:
    header = ["id","record_type","created_at","caller","name","address",
              "appointment_start","appointment_end","note","calendar_link","opted_out"]
    rows = []
    p = _day_path(day)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                j = json.loads(line)
                rows.append([
                    j.get("id",""), j.get("type",""), j.get("created_at",""),
                    j.get("caller","") or "", j.get("name","") or "", j.get("address","") or "",
                    j.get("start",""), j.get("end",""),
                    j.get("note","") or "", j.get("calendar_link","") or "",
                    "Yes" if j.get("type") == "optout" else "No"
                ])
            except Exception:
                continue
    out = [",".join(header)]
    for r in rows:
        out.append(",".join('\"{}\"'.format(str(c).replace('"','""')) for c in r))
    return "\n".join(out)

def save_booking(start_dt: datetime, caller: str | None, name: str | None, address: str | None, duration_min: int = 30):
    end_dt = start_dt + timedelta(minutes=duration_min)
    rec = {
        "type": "booking",
        "id": uuid.uuid4().hex[:12],
        "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        "start": start_dt.isoformat(), "end": end_dt.isoformat(),
        "caller": caller, "name": (name or "").strip(), "address": (address or "").strip(),
        "note": "Consultation", "calendar_link": ""
    }
    ics_text = make_ics(rec["id"], start_dt, end_dt, "Foreclosure Relief Consultation",
                        f"Caller: {caller or 'unknown'}; Name: {rec['name']}; Address: {rec['address']}")
    (ICS_DIR / f"{rec['id']}.ics").write_text(ics_text, encoding="utf-8")
    rec["calendar_link"] = f"/ics/{rec['id']}.ics"
    _write_jsonl_for_day(start_dt.date(), rec)
    mirror = rec.copy(); mirror["note"] = (mirror.get("note","") + ("; " if mirror.get("note") else "") + "mirror=true")
    _write_jsonl_for_day(datetime.now(TZ).date(), mirror)
    return rec

def save_optout(caller: str | None, name: str | None, address: str | None):
    rec = {
        "type": "optout", "id": uuid.uuid4().hex[:12],
        "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        "start": "", "end": "",
        "caller": caller, "name": (name or "").strip(), "address": (address or "").strip(),
        "note": "DNC request", "calendar_link": ""
    }
    _write_jsonl_for_day(datetime.now(TZ).date(), rec)
    return rec

WEEKDAYS = {w.lower(): i for i, w in enumerate(["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"])}

def parse_date_time(utt: str, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(TZ)
    s = utt.strip().lower()
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if not m: return None
    hh = int(m.group(1)); mm = int(m.group(2) or 0); ap = (m.group(3) or "").lower()
    if ap == "pm" and hh < 12: hh += 12
    if ap == "am" and hh == 12: hh = 0
    day = None
    if "tomorrow" in s:
        day = now.date() + timedelta(days=1)
    else:
        for name, idx in WEEKDAYS.items():
            if name in s:
                ahead = (idx - now.weekday()) % 7
                if ahead == 0 and (hh,mm) <= (now.hour, now.minute): ahead = 7
                day = now.date() + timedelta(days=ahead); break
    if not day: day = now.date() + timedelta(days=2)
    return datetime.combine(day, time(hh, mm), TZ)

def when_phrase(dt: datetime) -> str:
    return dt.strftime("%A, %B %d at %I:%M %p ") + TZ.key.split("/")[-1]

def propose_future_slots() -> list[datetime]:
    now = datetime.now(TZ)
    d1 = now.date() + timedelta(days=2)
    if d1.weekday() >= 5: d1 = d1 + timedelta(days=(7 - d1.weekday()))
    d2 = d1 + timedelta(days=1)
    return [datetime.combine(d1, time(11,0), TZ), datetime.combine(d2, time(14,0), TZ)]

async def send_text(ws: WebSocket, text: str):
    await ws.send_json({"type":"text","token":text,"last":True})

# ---------- HTTP ----------
@app.post("/voice")
async def voice(_: Request):
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <ConversationRelay
      url="{RELAY_WSS_URL}"
      ttsProvider="Amazon"
      voice="{VOICE_OUT}"
      language="en-US"
      interruptible="any"
      reportInputDuringAgentSpeech="speech"
      welcomeGreeting="Hi, I’m Chloe. How can I help?"
    />
  </Connect>
</Response>"""
    return PlainTextResponse(twiml, media_type="text/xml")

@app.get("/")
async def index(): return PlainTextResponse("OK")

@app.get("/health")
async def health(): return JSONResponse({"status":"ok"})

@app.get("/reports/{day}")
async def report_for_day(day: str):
    try: d = date.fromisoformat(day)
    except Exception: return PlainTextResponse("Bad date", status_code=400)
    return PlainTextResponse(render_report_csv(d), media_type="text/csv")

@app.get("/reports/today")
async def report_today():
    return PlainTextResponse(render_report_csv(datetime.now(TZ).date()), media_type="text/csv")

@app.get("/ics/{uid}.ics")
async def ics(uid: str):
    p = ICS_DIR / f"{uid}.ics"
    if not p.exists(): return PlainTextResponse("Not found", status_code=404)
    return PlainTextResponse(p.read_text(encoding="utf-8"), media_type="text/calendar")

# ---------- WebSocket ----------
@app.websocket("/relay")
async def relay(ws: WebSocket):
    await ws.accept()
    print("ConversationRelay: connected", flush=True)

    history: list[dict] = []
    state = {"mode":"chat","offered":[], "caller":None, "name":None, "address":None, "dt":None}

    try:
        while True:
            msg = await ws.receive_json()
            tp = msg.get("type")

            if tp == "setup":
                state["caller"] = normalize_phone(msg.get("from"))
                continue

            if tp == "prompt":
                user = (msg.get("voicePrompt") or "").strip()
                if not user: continue
                print("RX:", user, flush=True)

                # Opt-out anytime
                if re.search(r"\b(stop|do not call|don\'t call|remove me|opt[- ]?out)\b", user, re.I):
                    if not state["name"]:
                        await send_text(ws, "I can mark do-not-contact. What is your full name?"); continue
                    if not state["address"]:
                        await send_text(ws, "Thanks. What is the full property address from the notice?"); continue
                    await send_text(ws, f"Confirm do-not-contact for {state['name']} at {state['address']}, phone {state['caller'] or 'unknown'}. Is that correct?")
                    state["mode"]="confirm_optout"; continue

                # Passive capture
                m = re.search(r"\bmy name is\s+([A-Za-z][A-Za-z\s\-']{1,60})", user, re.I)
                if m and not state["name"]: state["name"] = m.group(1).strip()
                m = re.search(r"\b(address is|property at|the address is)\s*[:,]?\s*(.+)", user, re.I)
                if m and not state["address"]:
                    cand = m.group(2).strip().rstrip(".")
                    if len(cand) >= 5: state["address"] = cand

                # Booking trigger
                want_booking = bool(re.search(r"\b(book|schedule|appointment|set up|meeting|consult)\b", user, re.I))
                gave_time = bool(re.search(r"\b(mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow)\b", user, re.I)) and re.search(r"\d", user)
                if state["mode"] == "chat" and (want_booking or gave_time):
                    dt = parse_date_time(user)
                    if dt and dt > datetime.now(TZ):
                        state["dt"] = dt
                        if not state["name"]:
                            await send_text(ws, "Got it. What’s your full name?"); state["mode"]="booking"; continue
                        if not state["address"]:
                            await send_text(ws, "Thanks. What’s the full property address from the notice?"); state["mode"]="booking"; continue
                        when = when_phrase(state["dt"])
                        await send_text(ws, f"To confirm: {state['name']} at {state['address']} on {when}. Is that correct?")
                        state["mode"]="confirm_booking"; continue
                    else:
                        opts = propose_future_slots(); state["offered"]=opts
                        await send_text(ws, f"I can offer two times: first, {when_phrase(opts[0])}. Second, {when_phrase(opts[1])}. Which works?")
                        state["mode"]="booking"; continue

                if state["mode"] == "booking":
                    low = user.lower()
                    if ("first" in low) or low.strip() in ("1","one"): state["dt"] = state["offered"][0]
                    elif ("second" in low) or low.strip() in ("2","two"): state["dt"] = state["offered"][1]
                    if not state["dt"]:
                        dt = parse_date_time(user)
                        if dt and dt > datetime.now(TZ): state["dt"] = dt
                    if not state["name"]:
                        await send_text(ws, "What’s your full name?"); continue
                    if not state["address"]:
                        await send_text(ws, "What’s the full property address from the notice?"); continue
                    if not state["dt"]:
                        await send_text(ws, "What day and time should I book? (e.g., Tuesday at 2 PM)"); continue
                    when = when_phrase(state["dt"])
                    await send_text(ws, f"To confirm: {state['name']} at {state['address']} on {when}. Is that correct?")
                    state["mode"]="confirm_booking"; continue

                if state["mode"] == "confirm_booking":
                    if re.search(r"\b(yes|correct|that\'s right|sounds good)\b", user, re.I):
                        save_booking(state["dt"], state["caller"], state["name"], state["address"])
                        await send_text(ws, f"All set. I’ve booked {when_phrase(state['dt'])}. Anything else?")
                        state.update({"mode":"chat","offered":[],"dt":None}); continue
                    else:
                        await send_text(ws, "No problem—what should I change (name, address, or time)?")
                        state["mode"]="booking"; continue

                if state["mode"] == "confirm_optout":
                    if re.search(r"\b(yes|correct|that\'s right)\b", user, re.I):
                        save_optout(state["caller"], state["name"], state["address"])
                        await send_text(ws, "You’re marked do-not-contact. Anything else?")
                        state.update({"mode":"chat"}); continue
                    else:
                        await send_text(ws, "Okay—what should I correct (name or address)?")
                        state["mode"]="optout"; continue

                # Normal chat
                try:
                    resp = client.responses.create(
                        model="gpt-4.1-mini",
                        input=[{"role":"system","content":SYSTEM_PROMPT}, *history[-6:], {"role":"user","content":user}],
                        max_output_tokens=180, temperature=0.3
                    )
                    ai = (resp.output_text or "").strip() or "Sorry, could you repeat that?"
                except Exception as e:
                    print("OpenAI error:", repr(e), flush=True)
                    ai = "I’m having trouble right now. Please say that again."

                print("TX:", ai, flush=True)
                history.append({"role":"user","content":user}); history.append({"role":"assistant","content":ai})
                await send_text(ws, ai); continue

            if tp == "interrupt":
                print("Interrupted:", msg.get("utteranceUntilInterrupt", ""), flush=True); continue
            if tp == "error":
                print("ConversationRelay error:", msg.get("description"), flush=True); continue

    except WebSocketDisconnect:
        print("ConversationRelay: disconnected", flush=True)
