# app.py — FastAPI + Twilio Conversation Relay + OpenAI Responses (text frames)
# + Cal.com v2 in-call booking (Option B)

import os
import re
import json
import asyncio
import httpx

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from openai import OpenAI
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dateutil import parser as dateparse
# ---------- required env ----------
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
RELAY_WSS_URL  = os.environ["RELAY_WSS_URL"]   # wss://<your-app>.onrender.com/relay

# Cal.com (add these in Render dashboard > Environment)
CAL_TOKEN      = os.environ.get("CALCOM_API_TOKEN")       # required for booking
CAL_USERNAME   = os.environ.get("CALCOM_USERNAME")        # required
CAL_EVENT_SLUG = os.environ.get("CALCOM_EVENT_SLUG")      # required
CAL_TZ         = os.environ.get("CALCOM_TIMEZONE", "America/Los_Angeles")

client = OpenAI(api_key=OPENAI_API_KEY)
app = FastAPI()

SYSTEM_PROMPT = (
    "You are Chloe from Foreclosure Relief Group. Be warm, concise, and clear. "
    "Prefer 1–3 short sentences. Avoid filler. Offer more detail only if asked. "
    "If the caller wants to book, confirm the day/time and keep the language short."
    "\n\nWhen the caller explicitly confirms a date and time for an appointment, "
    "append ONE final line in EXACTLY this format (JSON on one line):\n"
    "BOOK_JSON: {\"start\":\"YYYY-MM-DDTHH:MM\", \"name\":\"Caller Name\"}\n"
    "Use the caller’s local time in the business timezone. "
    "Do not output BOOK_JSON until the caller has clearly confirmed both date and time."
)

# ---------- small helpers ----------
def normalize_phone(e164: str | None) -> str | None:
    if not e164:
        return None
    return re.sub(r"[^+0-9]", "", e164) or None

async def send_text(ws: WebSocket, text: str):
    # Conversation Relay expects 'type':'text' frames for TTS
    await ws.send_json({"type": "text", "token": text, "last": True})

# ---------- Cal.com v2 helpers ----------
CAL_BASE = "https://api.cal.com/v2"

CAL_HEADERS = {
    "Authorization": f"Bearer {CAL_TOKEN}" if CAL_TOKEN else "",
    "cal-api-version": "2024-08-13",
    "Content-Type": "application/json",
}

async def cal_get_slots(start_date: str, end_date: str) -> list[str]:
    """
    Return available slot starts (ISO strings) between start_date and end_date (YYYY-MM-DD).
    Uses eventTypeSlug + username + timeZone.
    """
    if not (CAL_TOKEN and CAL_USERNAME and CAL_EVENT_SLUG):
        return []
    url = "https://api.cal.com/v2/slots"
    params = {
        "username": CAL_USERNAME,
        "eventTypeSlug": CAL_EVENT_SLUG,
        "start": start_date,
        "end": end_date,
        "timeZone": CAL_TZ,
    }
    async with httpx.AsyncClient(timeout=15) as x:
        r = await x.get(url, headers=CAL_HEADERS, params=params)
    try:
        j = r.json()
    except Exception:
        return []
    if r.status_code == 200 and j.get("status") == "success":
        out = []
        for _day, arr in (j.get("data") or {}).items():
            for it in arr or []:
                if "start" in it:
                    out.append(it["start"])
        return out
    return []

async def cal_create_booking(start_iso: str, name: str, phone: str | None, duration_min: int = 30):
    """
    POST /v2/bookings to actually create the meeting.
    Returns dict: {"ok": bool, "id": "...", "error": "..."}
    """
    if not (CAL_TOKEN and CAL_USERNAME and CAL_EVENT_SLUG):
        return {"ok": False, "error": "Missing Cal.com env vars"}
    # normalize start in org TZ and compute end
    start_dt = dateparse.parse(start_iso)
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=ZoneInfo(CAL_TZ))
    end_dt = start_dt + timedelta(minutes=duration_min)

    payload = {
        "eventTypeSlug": CAL_EVENT_SLUG,
        "username": CAL_USERNAME,
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "timeZone": CAL_TZ,
        "durationInMinutes": duration_min,
        "attendee": {
            "name": name,
            # email is optional if your event type doesn’t require it
            **({"phoneNumber": phone} if phone else {}),
        },
    }
    url = "https://api.cal.com/v2/bookings"
    async with httpx.AsyncClient(timeout=20) as x:
        r = await x.post(url, headers=CAL_HEADERS, json=payload)
    try:
        j = r.json()
    except Exception:
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    if r.status_code == 200 and j.get("status") == "success":
        bid = (j.get("data") or {}).get("id") or (j.get("data") or {}).get("uid")
        return {"ok": True, "id": str(bid)}
    return {"ok": False, "error": j.get("message") or str(j) }

BOOK_RE = re.compile(r'BOOK_JSON:\s*(\{.*\})\s*$', re.IGNORECASE | re.DOTALL)

def extract_booking(json_line: str) -> dict | None:
    m = BOOK_RE.search(json_line)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        # expect keys: start, name
        if "start" in data and "name" in data:
            return data
    except Exception:
        pass
    return None


# ---------- HTTP: Twilio hits /voice ----------
@app.post("/voice")
async def voice(_: Request):
    # Tell Twilio to open a WebSocket to our /relay endpoint (Amazon Polly voice happens on Twilio’s side)
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

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})

# ---------- WebSocket: Twilio connects here ----------
@app.websocket("/relay")
async def relay(ws: WebSocket):
    await ws.accept()
    print("ConversationRelay: connected", flush=True)

    # tiny rolling history for coherence (kept short for latency)
    history: list[dict] = []

    # booking state
    state = {
        "mode": "chat",         # "chat" or "pick"
        "offered": [],          # list of ISO strings presented
        "caller_name": None,    # optional future enhancement
        "caller_phone": None,   # filled from setup frame
    }

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            mtype = msg.get("type")

            # Twilio sends this once at call start with caller info
            if mtype == "setup":
                state["caller_phone"] = normalize_phone(msg.get("from"))
                continue


            # Recognized speech from caller
            if mtype == "prompt":
                user_text = (msg.get("voicePrompt") or "").strip()
                if not user_text:
                    continue
                print("RX:", user_text, flush=True)

                # ====== Booking step 2: in "pick" mode, confirm which slot ======
                if state["mode"] == "pick":
                    choice = user_text.lower()
                    picked = None
                    if ("first" in choice) or ("1" in choice):
                        picked = state["offered"][0]
                    elif ("second" in choice) or ("2" in choice):
                        picked = state["offered"][1]
                    else:
                        # crude match if the caller repeats part of the ISO
                        for iso in state["offered"]:
                            if iso[:16] in choice:
                                picked = iso
                                break

                    if not picked:
                        await send_text(ws, "Sorry—was that the first time or the second?")
                        continue

                    # Try to book
                    await send_text(ws, "Great—booking that now, one moment.")
                    try:
                        details = await cal_create_booking(
                            start_iso=picked,
                            name=state["caller_name"] or "Caller",
                            phone=state["caller_phone"] or "unknown",
                        )
                        # Best-effort: confirm when; fall back to what we asked for
                        when = (
                            details.get("booking", {}).get("startTime")
                            or details.get("start")
                            or picked
                        )
                        await send_text(ws, f"All set. I’ve booked your appointment on {when}. Anything else?")
                    except Exception as e:
                        print("Cal.com booking error:", repr(e), flush=True)
                        await send_text(ws, "I couldn’t finish the booking just now. Want me to try another time?")
                    # Reset to chat
                    state["mode"] = "chat"
                    state["offered"] = []
                    continue

                # ====== Booking step 1: detect intent and fetch options ======
                want_booking = bool(re.search(r"\b(book|schedule|appointment|set up|meeting|consult)\b", user_text, re.I))
                if want_booking:
                    # If Cal.com credentials are missing, keep the convo graceful
                    if not (CAL_TOKEN and CAL_USERNAME and CAL_EVENT_SLUG):
                        await send_text(ws, "I can help schedule, but my calendar isn’t connected yet. Anything else?")
                        continue

                    await send_text(ws, "Happy to help. Let me find the next available times.")
                    try:
                        now_local = datetime.now(ZoneInfo(CAL_TZ)).date()
                        start = now_local.isoformat()
                        end   = (now_local + timedelta(days=14)).isoformat()
                        slots = await cal_get_slots(start, end)

                        if len(slots) < 2:
                            await send_text(ws, "I couldn’t find two open times right now. Would you like me to try again?")
                            continue

                        state["offered"] = slots[:2]
                        await send_text(ws, f"I have two options. First: {slots[0]}. Second: {slots[1]}. Which works?")
                        state["mode"] = "pick"

                    except Exception as e:
                        print("Error fetching slots:", repr(e), flush=True)
                        await send_text(ws, "Sorry, I couldn’t check the calendar right now. Please try again later.")
                    continue

                # ---------- normal chat via OpenAI ----------
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
                        book = extract_booking(ai_text)
                        if book:
                            # remove the BOOK_JSON line from what the caller hears
                            ai_text = BOOK_RE.sub("", ai_text).strip()
                            try:
                                req_local = dateparse.parse(book["start"])
                                if req_local.tzinfo is None:
                                    req_local = req_local.replace(tzinfo=ZoneInfo(CAL_TZ))

                                # check availability on that day
                                day_start = req_local.date().isoformat()
                                day_end   = (req_local.date() + timedelta(days=1)).isoformat()
                                slots = await cal_get_slots(day_start, day_end)

                                wanted_ok = any(s.startswith(req_local.isoformat()[:16]) for s in slots)
                                if not wanted_ok and slots:
                                    ai_text = (ai_text + "\n" if ai_text else "") + \
                                            f"That time isn’t available. Next openings are {slots[0]} or {slots[1]}. Which works?"
                                else:
                                    res = await cal_create_booking(
                                        req_local.isoformat(),
                                        name=book["name"],
                                        phone=state.get("caller_phone"),
                                    )
                                    if res["ok"]:
                                        when_say = req_local.strftime("%A %b %d at %I:%M %p")
                                        ai_text = (ai_text + "\n" if ai_text else "") + \
                                                f"All set — you’re booked for {when_say}."
                                    else:
                                        ai_text = (ai_text + "\n" if ai_text else "") + \
                                                f"Sorry, I couldn’t finalize that: {res['error']}."
                            except Exception:
                                ai_text = (ai_text + "\n" if ai_text else "") + \
                                        "I couldn’t parse that time. Could you say the date and time again, like ‘Tuesday at 2 PM’?"

                except Exception as e:
                    print("OpenAI error:", repr(e), flush=True)
                    ai_text = "I’m having trouble right now. Please say that again."

                print("TX:", ai_text, flush=True)

                # keep short context
                history.append({"role": "user", "content": user_text})
                history.append({"role": "assistant", "content": ai_text})

                await send_text(ws, ai_text)
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