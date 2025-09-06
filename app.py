import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import PlainTextResponse, JSONResponse

# Will crash at startup if not set (so make sure it's configured on Render!)
RELAY_WSS_URL = os.environ["RELAY_WSS_URL"]

app = FastAPI()

# ---------- HTTP: /voice (Twilio hits this) ----------
@app.post("/voice")
async def voice(_: Request):
    # ConversationRelay TwiML points Twilio at our /relay WebSocket
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

# Optional sanity endpoints
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
    try:
        while True:
            data = await ws.receive_text()
            print("RX:", (data[:500] + ("…" if len(data) > 500 else "")))
            # No replies yet — this step is just to prove the socket works
    except WebSocketDisconnect:
        print("ConversationRelay: disconnected")
