import os
import re
import json
import httpx
from datetime import datetime, timezone
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from openai import OpenAI
from twilio.rest import Client as TwilioRest

# ---- Env & clients ----
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
RELAY_WSS_URL = os.environ.get("RELAY_WSS_URL")  # wss://.../relay  (Render env)
TTS_VOICE = os.environ.get("TTS_VOICE", "Joanna-Neural")           # Amazon Polly voice name
CALCOM_USERNAME = os.environ.get("CALCOM_USERNAME", "")            # e.g., 'foreclosure-relief'
CALCOM_EVENT_SLUG = os.environ.get("CALCOM_EVENT_SLUG", "")        # e.g., 'consultation-15'
BOOKING_URL_BASE = os.environ.get("CALCOM_BOOKING_URL") or (f"https://cal.com/{CALCOM_USERNAME}/{CALCOM_EVENT_SLUG}" if CALCOM_USERNAME and CALCOM_EVENT_SLUG else None)

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_MESSAGING_FROM = os.environ.get("TWILIO_MESSAGING_FROM")  # your SMS-enabled Twilio number

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is required")
if not RELAY_WSS_URL:
    raise RuntimeError("RELAY_WSS_URL is required")

# Optional Twilio SMS client
twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_MESSAGING_FROM:
    twilio_client = TwilioRest(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = (
    "You are Chloe, a warm, concise phone assistant for Foreclosure Relief Group. "
    "Prioritize brevity (2–4 sentences). Use active-listening cues ('Got it', 'Understood'), "
    "and speak naturally and slowly. If caller asks to book an appointment, say you can text a secure booking link."
)

app = FastAPI()

def extract_text(msg: dict) -> str | None:
    # ConversationRelay prompt frames look like: {'type':'prompt','voicePrompt':'text',...}
    if not isinstance(msg, dict):
        return None
    if msg.get("type") == "prompt":
        return str(msg.get("voicePrompt", "")).strip()
    return None

async def say(ws: WebSocket, text: str):
    await ws.send_json({
        "type": "response",
        "actions": [{
            "say": text,
            "barge": True,
            "voice": {"name": TTS_VOICE, "provider": "amazon_polly"}
        }]
    })

def normalize_phone(e164: str | None) -> str | None:
    if not e164:
        return None
    # keep + and digits only
    digits = re.sub(r"[^+0-9]", "", e164)
    return digits or None

# ---------- HTTP: /voice (Twilio hits this) ----------
@app.post("/voice")
async def voice(_: Request):
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <ConversationRelay
      url="{RELAY_WSS_URL}"
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

# ---------- WebSocket: /relay (Twilio connects here) ----------
@app.websocket("/relay")
async def relay(ws: WebSocket):
    await ws.accept()
    print("ConversationRelay: connected")

    # rolling history for coherence without growing unbounded
    history: list[dict] = []

    caller_number: str | None = None

    try:
        while True:
            msg = await ws.receive_json()
            if isinstance(msg, dict) and msg.get("type") == "setup":
                caller_number = normalize_phone(msg.get("from"))
                continue

            text = extract_text(msg)
            if not text:
                continue

            print("RX:", text)

            # Very simple booking intent matcher
            want_booking = bool(re.search(r"book|appointment|schedule|set up|meeting", text, re.I))

            if want_booking and BOOKING_URL_BASE and twilio_client and caller_number:
                # Text the booking link to the caller
                try:
                    link = BOOKING_URL_BASE
                    twilio_client.messages.create(
                        from_=TWILIO_MESSAGING_FROM,
                        to=caller_number,
                        body=f"Hi, this is Chloe from Foreclosure Relief Group. "
                             f"Please choose a time here: {link}"
                    )
                    await say(ws, "Got it. I just texted you our booking link. "
                                  "You can pick a time that works best. "
                                  "Do you want me to stay on the line while you look, or answer anything else?")
                except Exception as sms_err:
                    print("SMS error:", sms_err)
                    await say(ws, "I tried to text the booking link but hit a snag. "
                                  "Would you like me to read the link out loud?")
                continue

            # ---- OpenAI Responses (text-in → text-out) ----
            try:
                resp = client.responses.create(
                    model="gpt-4o-mini",
                    input=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        *history[-6:],  # last 3 turns (user+assistant pairs)
                        {"role": "user", "content": text},
                    ],
                    max_output_tokens=180,
                )
                ai_text = resp.output_text.strip()
            except Exception as e:
                print("OpenAI error:", e)
                ai_text = "Sorry, I had trouble retrieving that. Could you please repeat or ask another way?"

            print("TX:", ai_text)

            # update short history
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": ai_text})

            await say(ws, ai_text)

    except WebSocketDisconnect:
        print("ConversationRelay: disconnected")
    finally:
        await ws.close()
