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

async def cal_get_slots(limit: int = 2) -> list[str]:
    """Return next available start ISO datetimes for the event."""
    if not (CAL_TOKEN and CAL_USERNAME and CAL_EVENT_SLUG):
        return []
    headers = {"Authorization": f"Bearer {CAL_TOKEN}"}
    params = {
        "username": CAL_USERNAME,
        "eventTypeSlug": CAL_EVENT_SLUG,
        "timeZone": CAL_TZ,
    }
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.get(f"{CAL_BASE}/slots", headers=headers, params=params)
    r.raise_for_status()
    data = r.json() or {}
    slots = [s.get("start") for s in data.get("slots", []) if s.get("start")]
    return slots[:limit]

async def cal_create_booking(start_iso: str, name: str, phone: str) -> dict:
    """Create a booking for the chosen start time."""
    headers = {
        "Authorization": f"Bearer {CAL_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "start": start_iso,
        "timeZone": CAL_TZ,
        "eventTypeSlug": CAL_EVENT_SLUG,
        "username": CAL_USERNAME,
        "attendee": {
            "name": name or "Phone Caller",
            # Cal.com needs an email; use a non-deliverable placeholder tied to the phone
            "email": f"{(phone or 'caller').lstrip('+')}@example.invalid",
        },
    }
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(f"{CAL_BASE}/bookings", headers=headers, json=payload)
    r.raise_for_status()
    return r.json()

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
                # We already sent a welcome via TwiML
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
                        slots = await cal_get_slots(limit=2)
                    except Exception as e:
                        print("Cal.com slots error:", repr(e), flush=True)
                        slots = []

                    if len(slots) < 2:
                        await send_text(ws, "I couldn’t find two open times right now. Would you like me to try again?")
                        continue

                    state["offered"] = slots
                    # Keep it short—Cal.com will handle proper calendar invites/timezone on the booked event
                    await send_text(ws, f"I have two options. First: {slots[0]}. Second: {slots[1]}. Which works?")
                    state["mode"] = "pick"
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