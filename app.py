import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import PlainTextResponse, JSONResponse

# ----- env -----
RELAY_WSS_URL = os.environ["RELAY_WSS_URL"]  # already added in Step 1

app = FastAPI()

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
            # Twilio sends JSON text frames (and sometimes pings)
            data = await ws.receive_text()
            # For now, just log the first 500 chars so we can verify traffic
            print("RX:", (data[:500] + ("…" if len(data) > 500 else "")))

            # (No reply yet—this step is just to prove the socket is working.
            #  If Twilio sends a ping, uvicorn handles the pong automatically.)
    except WebSocketDisconnect:
        print("ConversationRelay: disconnected")
