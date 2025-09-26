from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os, json, uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.responses import PlainTextResponse, JSONResponse

from openai import OpenAI

"""
app.py — Twilio ConversationRelay ↔ OpenAI Realtime bridge (September 2025)

This module connects Twilio’s ConversationRelay (CR) voice calls to OpenAI’s
Realtime API to build a voice AI assistant.  It listens for WebSocket events
from Twilio, forwards user prompts to the model, streams the model’s response
back token‑by‑token, and executes model‑requested tools such as booking an
appointment or marking a caller as do‑not‑contact.

Key features:
  • Minimal TwiML: only a <ConversationRelay> with two <Language> codes.
  • Uses OpenAI’s Realtime API (gpt‑4o‑realtime) via an async context manager;
    the entire CR↔OpenAI loop lives inside the `async with` block.  Do not call
    __aenter__ or __aexit__ yourself—this caused crashes in earlier versions【548398082812174†L307-L317】.
  • Handles English and Spanish; system prompts are chosen per call.
  • Supports two tools the model can call:
      – book_appointment(name?, address?, iso_start, duration_min?) → writes a
        JSONL record and iCalendar (.ics) file.
      – mark_opt_out(name?, address?, phone?) → records a do‑not‑contact entry.
  • Provides /voice for TwiML, /health for liveness, /ics/<id>.ics to fetch
    calendar files, and /reports/today for CSV reporting.

Environment variables expected:
  OPENAI_API_KEY  – your OpenAI API key
  RELAY_WSS_URL   – the public wss:// URL pointing back to this server’s /relay
  TIMEZONE        – IANA timezone string (e.g. America/Los_Angeles)
  VECTOR_STORE_ID – optional; ID of your OpenAI vector store for file_search
  ORG_NAME        – the name of your organisation to appear in prompts
"""

# ---------- Load configuration ----------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    print("WARNING: OPENAI_API_KEY not set", flush=True)

RELAY_WSS_URL = os.environ.get("RELAY_WSS_URL", "wss://YOUR-APP.onrender.com/relay")
BUSINESS_TZ = os.environ.get("TIMEZONE", "America/Los_Angeles")
VECTOR_STORE_ID = os.environ.get("VECTOR_STORE_ID", "")
ORG_NAME = os.environ.get("ORG_NAME", "Foreclosure Relief Group")

TZ = ZoneInfo(BUSINESS_TZ)

# Directories for storage
BOOK_DIR = Path(os.environ.get("BOOK_DIR", "/tmp/appointments"))
ICS_DIR = Path(os.environ.get("ICS_DIR", "/tmp/ics"))
REPORT_DIR = Path(os.environ.get("REPORT_DIR", "/tmp/reports"))
for p in (BOOK_DIR, ICS_DIR, REPORT_DIR):
    p.mkdir(parents=True, exist_ok=True)

# Initialise FastAPI and OpenAI client
app = FastAPI()
client = OpenAI(api_key=OPENAI_API_KEY)

# ---------- System prompts ----------
SYSTEM_PROMPT_EN = (
    "You are Chloe from " + ORG_NAME + ". Be concise and friendly (1‑3 sentences). "
    "Remain in English. When callers ask what this is about, briefly explain our services. "
    "Only call the 'book_appointment' tool when the caller clearly wants to schedule. "
    "If the caller asks to be removed, call 'mark_opt_out'. Confirm details once before saving. "
    "Avoid loops; if unclear, rephrase or move on."
)
SYSTEM_PROMPT_ES = (
    "Eres Chloe de " + ORG_NAME + ". Sé concisa y amable (1‑3 frases). "
    "Habla siempre en español. Si preguntan de qué se trata, explica brevemente nuestros servicios. "
    "Solo usa 'book_appointment' cuando la persona desee agendar. "
    "Si pide no ser contactada, usa 'mark_opt_out'. Confirma datos una sola vez antes de guardar. "
    "Evita bucles; si hay dudas, reformula o continúa."
)

def system_prompt_for(lang: str) -> str:
    """
    Return the appropriate system prompt based on a two‑letter language code.
    Defaults to English for any non‑Spanish code.
    """
    return SYSTEM_PROMPT_EN if (lang or "en").startswith("en") else SYSTEM_PROMPT_ES

# ---------- Helpers for storage ----------
def _day_file(d: datetime.date) -> Path:
    """Return the JSONL file path for a given date."""
    return BOOK_DIR / f"{d.isoformat()}.jsonl"

def _ics_timestamp(dt: datetime) -> str:
    """Format a datetime in UTC for iCalendar (YYYYMMDDTHHMMSSZ)."""
    u = dt.astimezone(ZoneInfo("UTC"))
    return u.strftime("%Y%m%dT%H%M%SZ")

def make_ics(uid: str, start_dt: datetime, end_dt: datetime, summary: str, description: str) -> str:
    """
    Create an iCalendar (VCALENDAR) string for an appointment.
    """
    nowz = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//FRG//Chloe//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}", f"DTSTAMP:{nowz}",
        f"DTSTART:{_ics_timestamp(start_dt)}",
        f"DTEND:{_ics_timestamp(end_dt)}",
        f"SUMMARY:{summary}", f"DESCRIPTION:{description}",
        "END:VEVENT", "END:VCALENDAR", ""
    ]
    return "\r\n".join(lines)

def write_row(rec: dict):
    """Append a record to today’s JSONL file."""
    p = _day_file(datetime.now(TZ).date())
    with p.open("a", encoding="utf‑8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def save_booking(start_iso: str, name: str | None, address: str | None, caller: str | None,
                 duration_min: int = 30) -> dict:
    """
    Save a booking record and generate an iCalendar file.
    """
    start_dt = datetime.fromisoformat(start_iso)
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=TZ)
    end_dt = start_dt + timedelta(minutes=duration_min)
    rec = {
        "type": "booking",
        "id": uuid.uuid4().hex[:12],
        "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "caller": caller or "",
        "name": (name or "").strip(),
        "address": (address or "").strip(),
        "note": "Consultation",
        "calendar_link": ""
    }
    ics_text = make_ics(
        rec["id"],
        start_dt,
        end_dt,
        f"{ORG_NAME} Consultation",
        f"Caller: {caller or 'unknown'}; Name: {rec['name']}; Address: {rec['address']}"
    )
    (ICS_DIR / f"{rec['id']}.ics").write_text(ics_text, encoding="utf‑8")
    write_row(rec)
    return rec

def save_optout(name: str | None, address: str | None, phone: str | None) -> dict:
    """
    Save a do‑not‑contact record.
    """
    rec = {
        "type": "optout",
        "id": uuid.uuid4().hex[:12],
        "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        "start": "", "end": "",
        "caller": phone or "",
        "name": (name or "").strip(),
        "address": (address or "").strip(),
        "note": "DNC request",
        "calendar_link": ""
    }
    write_row(rec)
    return rec

# ---------- TwiML endpoint ----------
@app.post("/voice")
async def voice(_: Request):
    """
    Return minimal TwiML instructing Twilio to open a ConversationRelay WebSocket.
    """
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <ConversationRelay
      url="{RELAY_WSS_URL}"
      ttsProvider="Amazon"
      voice="Joanna-Neural">
      <Language code="en-US"/>
      <Language code="es-US"/>
    </ConversationRelay>
  </Connect>
</Response>"""
    return PlainTextResponse(twiml, media_type="text/xml")

# ---------- Utility endpoints ----------
@app.get("/health")
async def health():
    """Liveness probe for Render; returns OK if the service is up."""
    return JSONResponse({"ok": True})

@app.get("/")
async def index():
    return PlainTextResponse("OK")

@app.get("/ics/{bid}.ics")
async def get_ics(bid: str):
    """
    Retrieve an .ics file by booking ID.
    """
    p = ICS_DIR / f"{bid}.ics"
    if p.exists():
        return PlainTextResponse(p.read_text(encoding="utf-8"), media_type="text/calendar; charset=utf-8")
    return Response(status_code=404)

@app.get("/reports/today")
async def report_today():
    """
    Export today’s records as CSV.
    """
    d = datetime.now(TZ).date()
    p = _day_file(d)
    if not p.exists():
        return PlainTextResponse("id,record_type,created_at,caller,name,address,appointment_start,appointment_end,note,calendar_link\n",
                                 media_type="text/csv")
    header = ["id","record_type","created_at","caller","name","address","appointment_start","appointment_end","note","calendar_link"]
    out = [",".join(header)]
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            j = json.loads(line)
            row = [
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
            ]
            safe = ['"{}"'.format(str(x).replace('"','""')) for x in row]
            out.append(",".join(safe))
        except Exception:
            continue
    return PlainTextResponse("\n".join(out), media_type="text/csv")

# ---------- ConversationRelay ↔ Realtime bridge ----------
async def send_text(ws: WebSocket, token: str, last: bool):
    """
    Send a text token to Twilio.  Twilio requires messages to conform to a strict
    shape: {"type": "text", "token": <string>, "last": <bool>}【891365421160775†L500-L548】.
    """
    await ws.send_json({"type": "text", "token": token, "last": last})

# Define tool specifications for the model
TOOLS_SPEC = [
    {
        "type": "function",
        "name": "book_appointment",
        "description": "Save a consultation with optional name/address and ISO8601 start time.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "address": {"type": "string"},
                "iso_start": {"type": "string", "description": "ISO8601 datetime with timezone, e.g., 2025-09-20T15:00:00-07:00"},
                "duration_min": {"type": "integer", "default": 30}
            },
            "required": ["iso_start"]
        }
    },
    {
        "type": "function",
        "name": "mark_opt_out",
        "description": "Record a do-not-contact request with optional name/address/phone.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "address": {"type": "string"},
                "phone": {"type": "string"}
            },
            "required": []
        }
    }
]

@app.websocket("/relay")
async def relay(ws: WebSocket):
    """
    Handle the ConversationRelay WebSocket connection.  This function:
      • Accepts the CR WebSocket.
      • Opens a single OpenAI Realtime session inside an async context manager【548398082812174†L307-L317】.
      • Forwards user prompts to the model.
      • Streams the model’s text response back to Twilio token‑by‑token.
      • Executes model‑requested tools and returns their results.
      • Handles language switches and interruptions.
    """
    await ws.accept()
    print("ConversationRelay: connected", flush=True)
    caller_number: str | None = None
    lang = "en"  # default language; switches when receiving language events

    try:
        # Use OpenAI Realtime session as an async context manager.
        async with client.realtime.connect(model="gpt-4o-realtime") as rt:
            # Set initial session configuration
            instructions = system_prompt_for(lang)
            await rt.session.update(session={
                "modalities": ["text"],
                "instructions": instructions,
                "tools": TOOLS_SPEC
            })

            # Main loop: handle incoming CR events
            while True:
                try:
                    msg = await ws.receive_json()
                except WebSocketDisconnect:
                    break
                mtype = msg.get("type")

                if mtype == "setup":
                    # Extract caller’s number (if available)
                    caller_number = (msg.get("from") or "").strip() or None
                    continue

                if mtype == "language":
                    # CR language switch event; update model instructions
                    code = (msg.get("language") or "").lower()
                    lang = "es" if code.startswith("es") else "en"
                    new_instructions = system_prompt_for(lang)
                    # Update session instructions
                    await rt.session.update(session={"instructions": new_instructions})
                    continue

                if mtype == "prompt":
                    # User said something; send to model
                    user_text = (msg.get("voicePrompt") or "").strip()
                    if not user_text:
                        continue
                    # Create user message
                    await rt.conversation.item.create(item={
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": user_text}]
                    })
                    # Request a model response
                    await rt.response.create()

                    # Stream back tokens to Twilio
                    async for event in rt.stream():
                        etype = event.get("type")
                        if etype == "response.text.delta":
                            await send_text(ws, event.get("delta") or "", False)
                        elif etype == "response.text.done":
                            await send_text(ws, "", True)
                            break
                        elif etype in ("response.error", "error"):
                            print("Realtime error:", event, flush=True)
                            break
                        elif etype == "response.function.call":
                            # The model called one of our tools
                            fn = event.get("name")
                            args = event.get("arguments") or {}
                            tool_id = event.get("tool_call_id") or event.get("id") or uuid.uuid4().hex
                            # Execute the function locally
                            try:
                                if fn == "book_appointment":
                                    rec = save_booking(
                                        start_iso=args.get("iso_start"),
                                        name=args.get("name"),
                                        address=args.get("address"),
                                        caller=caller_number,
                                        duration_min=int(args.get("duration_min") or 30)
                                    )
                                    result = {"ok": True, "id": rec["id"], "ics_url": f"/ics/{rec['id']}.ics"}
                                elif fn == "mark_opt_out":
                                    rec = save_optout(
                                        name=args.get("name"),
                                        address=args.get("address"),
                                        phone=caller_number or args.get("phone")
                                    )
                                    result = {"ok": True, "id": rec["id"]}
                                else:
                                    result = {"ok": False, "error": f"Unknown tool {fn}"}
                            except Exception as e:
                                result = {"ok": False, "error": repr(e)}

                            # Send the tool result back to the model
                            await rt.response.create(
                                response={"type": "tool_result", "tool_call_id": tool_id, "output": result}
                            )
                        elif etype in ("response.completed", "response.finish"):
                            break
                    continue

                if mtype == "interrupt":
                    # CR notifies that the user barge‑in occurred.  We simply log it and
                    # let Realtime handle turn‑taking natively.
                    print("Interrupted:", msg.get("utteranceUntilInterrupt", ""), flush=True)
                    continue

                if mtype == "error":
                    # CR error event; log and ignore
                    print("CR error:", msg.get("description"), flush=True)
                    continue

    except WebSocketDisconnect:
        pass
    finally:
        print("ConversationRelay: disconnected", flush=True)