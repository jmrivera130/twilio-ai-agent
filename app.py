# app.py  — FastAPI + Twilio Conversation Relay + OpenAI Responses (barge-in)
import os
from typing import Any, Dict, List, Union

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from openai import OpenAI

# --------- REQUIRED ENV VARS ----------
# On Render (or wherever you deploy), set:
#   OPENAI_API_KEY = sk-...
#   RELAY_WSS_URL  = wss://<your-render-app>.onrender.com/relay
# Optional:
#   TWILIO_VOICE   = Polly.Joanna-Neural  (or leave unset for Twilio default)
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
RELAY_WSS_URL = os.environ["RELAY_WSS_URL"]

client = OpenAI(api_key=OPENAI_API_KEY)
app = FastAPI()

# Chloe’s speaking style: fast, warm, concise (keeps phone UX snappy)
SYSTEM_PROMPT = (
    "You are Chloe from Foreclosure Relief Group. Be warm, concise, and clear. "
    "Prefer 1–3 short sentences. Avoid filler. If the caller needs more detail, "
    "offer to explain further instead of monologuing."
)

# ---------- HTTP: /voice (Twilio hits this) ----------
@app.post("/voice")
async def voice(_: Request):
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

# Optional sanity endpoints
@app.get("/")
async def index():
    return PlainTextResponse("OK")

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})

# ---------- helpers ----------
JsonLike = Union[Dict[str, Any], List[Any]]

def extract_text(obj: JsonLike) -> str:
    """
    Conversation Relay frames can vary. This pulls the first plausible
    transcript/text field from nested dicts/lists.
    """
    if isinstance(obj, dict):
        # Direct hits
        for key in ("text", "transcript"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()

        # Common nested shapes
        for key in ("speech", "asr", "input", "user"):
            if key in obj:
                got = extract_text(obj[key])
                if got:
                    return got

        # Fallback: scan all values
        for v in obj.values():
            got = extract_text(v)
            if got:
                return got

    elif isinstance(obj, list):
        for v in obj:
            got = extract_text(v)
            if got:
                return got

    return ""

# ---------- WebSocket: /relay (Twilio connects here) ----------
@app.websocket("/relay")
async def relay(ws: WebSocket):
    await ws.accept()
    print("ConversationRelay: connected", flush=True)

    voice = os.environ.get("TTS_VOICE", "Joanna-Neural")
    history: list[dict] = []

    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")

            # 1) Greet immediately on setup so you hear voice right away
            if mtype == "setup":
                # ---- Tell ConversationRelay to speak the text ----
                await ws.send_json({
                    "type": "text",
                    "token": ai_text,   # the text you want Chloe to speak
                    "last": True        # send True if this is a complete response (not streaming)
                })

             
                continue

            # 2) Handle recognized speech from the caller
            if mtype == "prompt":
                user_text = msg.get("voicePrompt", "")
                if not user_text.strip():
                    continue

                print("RX:", user_text, flush=True)

                # Call OpenAI (short, fast)
                try:
                    resp = client.responses.create(
                        model="gpt-4o-mini",
                        input=[
                            {"role": "system", "content": SYSTEM_PROMPT},
                            *history[-6:],
                            {"role": "user", "content": user_text},
                        ],
                        max_output_tokens=180,
                        temperature=0.3,
                    )
                    ai_text = (resp.output_text or "").strip()
                    if not ai_text:
                        ai_text = "Sorry, I didn’t catch that. Could you repeat?"
                except Exception as e:
                    print("OpenAI error:", repr(e), flush=True)
                    ai_text = "I’m having trouble right now. Please say that again."

                print("TX:", ai_text, flush=True)

                # keep a tiny rolling context
                history.append({"role": "user", "content": user_text})
                history.append({"role": "assistant", "content": ai_text})

                # **Correct schema** for ConversationRelay to speak
                await ws.send_json({
                    "type": "response",
                    "actions": [{
                        "say": ai_text,
                        "barge": True,
                        "voice": voice
                    }]
                })
                continue

            # 3) Caller interrupt while we’re speaking (optional logging)
            if mtype == "interrupt":
                print("Interrupted:", msg.get("utteranceUntilInterrupt", ""), flush=True)
                continue

            # 4) Errors from Twilio
            if mtype == "error":
                print("ConversationRelay error:", msg.get("description"), flush=True)
                continue

            # Ignore other frame types unless you need them

    except WebSocketDisconnect:
        print("ConversationRelay: disconnected", flush=True)
