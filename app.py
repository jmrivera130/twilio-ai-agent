# app.py — FastAPI + Twilio Conversation Relay + OpenAI Responses
# Local bookings (JSONL) + CSV reports + ICS downloads. No Cal.com.
# Pulls guidance from PDFs via OpenAI file_search (VECTOR_STORE_ID) and paraphrases.

import os
import re
import json
import uuid
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from openai import OpenAI

# ---------- env ----------
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
RELAY_WSS_URL  = os.environ["RELAY_WSS_URL"]   # wss://<app>.onrender.com/relay
TTS_VOICE      = os.environ.get("TTS_VOICE", "Joanna-Neural")
BUSINESS_TZ    = os.environ.get("TIMEZONE", os.environ.get("CALCOM_TIMEZONE", "America/Los_Angeles"))
VECTOR_STORE_ID = os.environ.get("VECTOR_STORE_ID")  # optional RAG

TZ = ZoneInfo(BUSINESS_TZ)

# Storage dirs
BOOK_DIR   = Path(os.environ.get("BOOK_DIR", "/tmp/appointments"))
REPORT_DIR = Path(os.environ.get("REPORT_DIR", "/tmp/reports"))
ICS_DIR    = Path(os.environ.get("ICS_DIR", "/tmp/ics"))
for d in (BOOK_DIR, REPORT_DIR, ICS_DIR):
    d.mkdir(parents=True, exist_ok=True)

client = OpenAI(api_key=OPENAI_API_KEY)
app = FastAPI()

SYSTEM_PROMPT = (
    "You are Chloe from Foreclosure Relief Group. Be warm, concise, and clear. "
    "Prefer 1–3 short sentences. Avoid filler. Offer more detail only if asked. "
    "Use internal knowledge (uploaded documents) as guidance to answer accurately; "
    "paraphrase in your own words and do not mention or quote documents. "
    "When scheduling, gather name, the property address in the notice, and day/time."
)

# ---------- helpers ----------
def normalize_phone(s: str | None) -> str | None:
    if not s:
        return None
    digits = re.sub(r"\D+", "", s)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"+1{digits}"
    return f"+{digits}" if digits else None

def _day_path(day: date) -> Path:
    return REPORT_DIR / f"appointments-{day.isoformat()}.jsonl"

def _write_jsonl_for_day(day: date, rec: dict):
    p = _day_path(day)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def make_ics(uid: str, start_dt: datetime, end_dt: datetime, summary: str, description: str) -> str:
    fmt = "%Y%m%dT%H%M%S"
    s = start_dt.strftime(fmt)
    e = end_dt.strftime(fmt)
    now = datetime.now(ZoneInfo("UTC")).strftime(fmt) + "Z"
    return ( "BEGIN:VCALENDAR\n"
             "VERSION:2.0\n"
             "PRODID:-//Chloe//FRG//EN\n"
             "BEGIN:VEVENT\n"
             f"UID:{uid}@chloe\n"
             f"DTSTAMP:{now}\n"
             f"DTSTART;TZID={TZ.key}:{s}\n"
             f"DTEND;TZID={TZ.key}:{e}\n"
             f"SUMMARY:{summary}\n"
             f"DESCRIPTION:{description}\n"
             "END:VEVENT\n"
             "END:VCALENDAR\n" )

def render_report_csv(day: date) -> str:
    header = ["id","record_type","created_at","caller","name","address",
              "appointment_start","appointment_end","note","calendar_link","opted_out"]
    p = _day_path(day)
    rows = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                j = json.loads(line)
            except Exception:
                continue
            rows.append([
                j.get("id",""),
                j.get("type",""),
                j.get("created_at",""),
                j.get("caller","") or "",
                j.get("name","") or "",
                j.get("address","") or "",
                j.get("start",""),
                j.get("end",""),
                j.get("note","") or "",
                j.get("calendar_link","") or "",
                "Yes" if j.get("type") == "optout" else "No",
            ])
    out = [",".join(header)]
    for r in rows:
        safe = ['"{}"'.format(str(x).replace('"','""')) for x in r]
        out.append(",".join(safe))
    return "\n".join(out)

def save_booking(start_dt: datetime, caller_number: str | None,
                 name: str | None, address: str | None,
                 note: str = "Consultation", duration_min: int = 30):
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
    ics_text = make_ics(rec["id"], start_dt, end_dt,
                        "Foreclosure Relief Consultation",
                        f"Caller: {caller_number or 'unknown'}; Name: {rec['name']}; Address: {rec['address']}")
    (ICS_DIR / f"{rec['id']}.ics").write_text(ics_text, encoding="utf-8")
    rec["calendar_link"] = f"/ics/{rec['id']}.ics"
    # Store under appointment date
    _write_jsonl_for_day(start_dt.date(), rec)
    # Mirror into today's file so EOD report shows both
    mirror = rec.copy()
    mirror["note"] = (mirror.get("note","") + ("; " if mirror.get("note") else "") + "mirror=true")
    _write_jsonl_for_day(datetime.now(TZ).date(), mirror)
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
    _write_jsonl_for_day(now_local.date(), rec)
    return rec

# ----- natural language time helpers -----
WEEKDAYS = {w.lower(): i for i, w in enumerate(["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"])}

def parse_date_time(utterance: str, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(TZ)
    s = utterance.strip().lower()
    # AM/PM and HH:MM
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    if ampm == "pm" and hh < 12:
        hh += 12
    if ampm == "am" and hh == 12:
        hh = 0
    # Day selection
    day = None
    if "tomorrow" in s:
        day = now.date() + timedelta(days=1)
    else:
        for name, idx in WEEKDAYS.items():
            if name in s:
                # compute next given weekday (could be today+7 if same-day already passed)
                days_ahead = (idx - now.weekday()) % 7
                if days_ahead == 0 and (hh,mm) <= (now.hour, now.minute):
                    days_ahead = 7
                day = now.date() + timedelta(days=days_ahead)
                break
    if not day:
        # default to next 2 days
        day = now.date() + timedelta(days=2)
    return datetime.combine(day, time(hour=hh, minute=mm), TZ)

def format_when(dt: datetime) -> str:
    return dt.strftime("%A, %B %d at %I:%M %p ") + TZ.key.split("/")[-1]

def propose_future_slots() -> list[datetime]:
    now = datetime.now(TZ)
    # Suggest in 2 and 3 days at friendly hours
    d1 = now.date() + timedelta(days=2)
    if d1.weekday() >= 5:  # weekend -> Monday
        d1 = d1 + timedelta(days=(7 - d1.weekday()))
    d2 = d1 + timedelta(days=1)
    return [
        datetime.combine(d1, time(11, 0), TZ),
        datetime.combine(d2, time(14, 0), TZ),
    ]

# ---------- HTTP: Twilio hits /voice ----------
@app.post("/voice")
async def voice(_: Request):
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <ConversationRelay
      url="{RELAY_WSS_URL}"
      ttsProvider="Amazon"
      voice="{TTS_VOICE}"
      interruptible="any"
      reportInputDuringAgentSpeech="speech"
      welcomeGreeting="Hi, I’m Chloe. How can I help?"
    />
  </Connect>
</Response>"""
    return PlainTextResponse(twiml, media_type="text/xml")

@app.get("/")
async def index():
    return PlainTextResponse("OK")

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})

@app.get("/reports/{day}")
async def get_report(day: str):
    # day format: YYYY-MM-DD
    try:
        d = date.fromisoformat(day)
    except Exception:
        return PlainTextResponse("Bad date", status_code=400)
    csv = render_report_csv(d)
    return PlainTextResponse(csv, media_type="text/csv")

@app.get("/ics/{uid}.ics")
async def ics(uid: str):
    p = ICS_DIR / f"{uid}.ics"
    if not p.exists():
        return PlainTextResponse("Not found", status_code=404)
    return PlainTextResponse(p.read_text(encoding="utf-8"), media_type="text/calendar")

# ---------- WebSocket: Twilio connects here ----------
@app.websocket("/relay")
async def relay(ws: WebSocket):
    await ws.accept()
    print("ConversationRelay: connected", flush=True)

    # tiny rolling history for coherence (kept short for latency)
    history: list[dict] = []

    state = {
        "mode": "chat",          # chat | booking | confirm | optout
        "offered": [],           # list[datetime]
        "caller_phone": None,
        "name": None,
        "address": None,
        "dt": None,              # datetime
    }

    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")

            if mtype == "setup":
                state["caller_phone"] = normalize_phone(msg.get("from"))
                continue

            if mtype == "prompt":
                user_text = (msg.get("voicePrompt") or "").strip()
                if not user_text:
                    continue
                print("RX:", user_text, flush=True)

                # Opt-out detection (anytime)
                if re.search(r"\b(stop|do not call|don\'t call|remove me|opt[- ]?out)\b", user_text, re.I):
                    state["mode"] = "optout"
                    if not state["name"]:
                        await ws.send_json({"type":"text","token":"I can mark do-not-contact. What is your full name?","last":True})
                        continue
                    if not state["address"]:
                        await ws.send_json({"type":"text","token":"Thanks. What is the full property address from the notice?","last":True})
                        continue
                    # Confirm opt-out
                    ph = state["caller_phone"] or "unknown"
                    await ws.send_json({"type":"text","token":f"Confirm do-not-contact for {state['name']} at {state['address']}, phone {ph}. Is that correct?","last":True})
                    state["mode"] = "confirm_optout"
                    continue

                # Passive capture (name/address)
                m = re.search(r"\bmy name is\s+([A-Za-z][A-Za-z\s\-']{1,60})", user_text, re.I)
                if m and not state["name"]:
                    state["name"] = m.group(1).strip()

                m = re.search(r"\b(address is|property at|the address is)\s*[:,]?\s*(.+)", user_text, re.I)
                if m and not state["address"]:
                    cand = m.group(2).strip().rstrip(".")
                    if len(cand) >= 5:
                        state["address"] = cand

                # Booking flow trigger
                want_booking = bool(re.search(r"\b(book|schedule|appointment|set up|meeting|consult)\b", user_text, re.I))
                gave_time = bool(re.search(r"\b(mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow)\b", user_text, re.I)) and re.search(r"\d", user_text)
                if state["mode"] == "chat" and (want_booking or gave_time):
                    # Try to parse a concrete time from the utterance
                    dt = parse_date_time(user_text)
                    if dt and dt > datetime.now(TZ):
                        state["dt"] = dt
                        if not state["name"]:
                            await ws.send_json({"type":"text","token":"Got it. What’s your full name?","last":True})
                            state["mode"] = "booking"
                            continue
                        if not state["address"]:
                            await ws.send_json({"type":"text","token":"Thanks. What’s the full property address from the notice?","last":True})
                            state["mode"] = "booking"
                            continue
                        # Have everything → confirm
                        when = format_when(state["dt"])
                        await ws.send_json({"type":"text","token":f"To confirm: {state['name']} at {state['address']} on {when}. Is that correct?","last":True})
                        state["mode"] = "confirm_booking"
                        continue
                    else:
                        # Offer two future options
                        opts = propose_future_slots()
                        state["offered"] = opts
                        msg1 = f"I can offer two times: first, {format_when(opts[0])}. Second, {format_when(opts[1])}. Which works?"
                        await ws.send_json({"type":"text","token":msg1,"last":True})
                        state["mode"] = "booking"
                        continue

                # In booking mode, fill missing slots or pick offered
                if state["mode"] == "booking":
                    # Choose first/second
                    low = user_text.lower()
                    if ("first" in low) or low.strip() in ("1","one"):
                        state["dt"] = state["offered"][0]
                    elif ("second" in low) or low.strip() in ("2","two"):
                        state["dt"] = state["offered"][1]
                    # Try parsing time if none chosen
                    if not state["dt"]:
                        dt = parse_date_time(user_text)
                        if dt and dt > datetime.now(TZ):
                            state["dt"] = dt
                    # Ask for name/address if missing
                    if not state["name"]:
                        await ws.send_json({"type":"text","token":"What’s your full name?","last":True})
                        continue
                    if not state["address"]:
                        await ws.send_json({"type":"text","token":"What’s the full property address from the notice?","last":True})
                        continue
                    if not state["dt"]:
                        await ws.send_json({"type":"text","token":"What day and time should I book? (e.g., Tuesday at 2 PM)","last":True})
                        continue
                    # Confirm
                    when = format_when(state["dt"])
                    await ws.send_json({"type":"text","token":f"To confirm: {state['name']} at {state['address']} on {when}. Is that correct?","last":True})
                    state["mode"] = "confirm_booking"
                    continue

                # Confirmation handlers
                if state["mode"] == "confirm_booking":
                    if re.search(r"\b(yes|correct|that\'s right|sounds good)\b", user_text, re.I):
                        rec = save_booking(
                            start_dt=state["dt"],
                            caller_number=state["caller_phone"],
                            name=state["name"],
                            address=state["address"],
                        )
                        when = format_when(state["dt"])
                        await ws.send_json({"type":"text","token":f"All set. I’ve booked {when}. Anything else?","last":True})
                        # Reset to chat
                        state.update({"mode":"chat","offered":[],"dt":None})
                        continue
                    else:
                        await ws.send_json({"type":"text","token":"No problem—what should I change (name, address, or time)?","last":True})
                        state["mode"] = "booking"
                        continue

                if state["mode"] == "confirm_optout":
                    if re.search(r"\b(yes|correct|that\'s right)\b", user_text, re.I):
                        save_optout(
                            caller_number=state["caller_phone"],
                            name=state["name"],
                            address=state["address"],
                        )
                        await ws.send_json({"type":"text","token":"You’re marked do-not-contact. Anything else?","last":True})
                        state.update({"mode":"chat"})
                        continue
                    else:
                        await ws.send_json({"type":"text","token":"Okay—what should I correct (name or address)?","last":True})
                        state["mode"] = "optout"
                        continue

                # ---------- normal chat via OpenAI ----------
                try:
                    kwargs = dict(
                        model="gpt-4o-mini",
                        input=[
                            {"role": "system", "content": SYSTEM_PROMPT},
                            *history[-6:],
                            {"role": "user", "content": user_text},
                        ],
                        max_output_tokens=180,
                        temperature=0.3,
                    )
                    if VECTOR_STORE_ID:
                        kwargs["attachments"] = [{"vector_store_id": VECTOR_STORE_ID}]
                        kwargs["tools"] = [{"type":"file_search"}]
                    resp = client.responses.create(**kwargs)
                    ai_text = (resp.output_text or "").strip()
                    if not ai_text:
                        ai_text = "Sorry, could you repeat that?"
                except Exception as e:
                    print("OpenAI error:", repr(e), flush=True)
                    ai_text = "I’m having trouble right now. Please say that again."

                print("TX:", ai_text, flush=True)
                history.append({"role": "user", "content": user_text})
                history.append({"role": "assistant", "content": ai_text})

                await ws.send_json({"type":"text","token":ai_text,"last":True})
                continue

            if mtype == "interrupt":
                print("Interrupted:", msg.get("utteranceUntilInterrupt", ""), flush=True)
                continue

            if mtype == "error":
                print("ConversationRelay error:", msg.get("description"), flush=True)
                continue

    except WebSocketDisconnect:
        print("ConversationRelay: disconnected", flush=True)
