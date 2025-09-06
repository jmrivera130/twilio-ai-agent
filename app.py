# app.py — FastAPI + Twilio Conversation Relay + OpenAI Responses (text frames)

import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from openai import OpenAI

# ---------- required env ----------
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
RELAY_WSS_URL  = os.environ["RELAY_WSS_URL"]   # wss://<your-app>.onrender.com/relay

client = OpenAI(api_key=OPENAI_API_KEY)
app = FastAPI()

SYSTEM_PROMPT = (
    "You are Chloe from Foreclosure Relief Group. Be warm, concise, and clear. "
    "Prefer 1–3 short sentences. Avoid filler. Offer more detail only if asked."
)

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

    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")

            # Twilio sends this once at call start
            if mtype == "setup":
                # We already send a welcome via TwiML, so do nothing here.
                continue

            # Recognized speech from caller
            if mtype == "prompt":
                user_text = (msg.get("voicePrompt") or "").strip()
                if not user_text:
                    continue
                print("RX:", user_text, flush=True)

                # Call OpenAI (fast model)
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

                # ✅ Correct frame for Conversation Relay: send plain text
                await ws.send_json({
                    "type": "text",
                    "token": ai_text,
                    "last": True
                })
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
