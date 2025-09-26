from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os, json, uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.responses import PlainTextResponse, JSONResponse

from openai import OpenAI

# ---------- Env & constants ----------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    print("WARNING: OPENAI_API_KEY not set", flush=True)

RELAY_WSS_URL = os.environ.get("RELAY_WSS_URL", "wss://YOUR-APP.onrender.com/relay")
BUSINESS_TZ = os.environ.get("TIMEZONE", "America/Los_Angeles")
TZ = ZoneInfo(BUSINESS_TZ)

# Storage
BOOK_DIR = Path(os.environ.get("BOOK_DIR", "/tmp/appointments"))
ICS_DIR = Path(os.environ.get("ICS_DIR", "/tmp/ics"))
REPORT_DIR = Path(os.environ.get("REPORT_DIR", "/tmp/reports"))
for p in [BOOK_DIR, ICS_DIR, REPORT_DIR]:
    p.mkdir(parents=True, exist_ok=True)

client = OpenAI(api_key=OPENAI_API_KEY)
app = FastAPI()

# System prompts
SYSTEM_PROMPT_EN = (
    "You are Chloe from Foreclosure Relief Group, a concise, friendly voice assistant. "
    "Speak in short sentences (1-3). Be patient; ask for clarification gently. "
    "Stay in English. If the caller asks 'what is this about', briefly explain services. "
    "Only when the caller clearly wants to book, call the tool 'book_appointment'. "
    "If they ask to be removed or say do not call, call the tool 'mark_opt_out'. "
    "Confirm details once before saving. Never loop; if uncertain, rephrase or move on."
)
SYSTEM_PROMPT_ES = (
    "Eres Chloe del Foreclosure Relief Group. Habla en español claro (1–3 frases). "
    "Mantente en español. Si preguntan '¿de qué se trata?', explica brevemente. "
    "Solo cuando la persona quiera agendar, llama a la herramienta 'book_appointment'. "
    "Si pide no ser contactada, llama a 'mark_opt_out'. Confirma una vez antes de guardar. "
    "Evita bucles; si hay duda, reformula o sigue."
)

def system_prompt_for(lang: str) -> str:
    return SYSTEM_PROMPT_EN if (lang or "en").startswith("en") else SYSTEM_PROMPT_ES

# ---------- Storage helpers ----------
def _day_path(day: datetime.date) -> Path:
    return BOOK_DIR / f"{day.isoformat()}.jsonl"

def _ics_dt(dt: datetime) -> str:
    u = dt.astimezone(ZoneInfo("UTC"))
    return u.strftime("%Y%m%dT%H%M%SZ")

def make_ics(uid: str, start_dt: datetime, end_dt: datetime, summary: str, description: str) -> str:
    nowz = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR","VERSION:2.0","PRODID:-//FRG//Chloe//EN","CALSCALE:GREGORIAN","METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}", f"DTSTAMP:{nowz}",
        f"DTSTART:{_ics_dt(start_dt)}",
        f"DTEND:{_ics_dt(end_dt)}",
        f"SUMMARY:{summary}", f"DESCRIPTION:{description}",
        "END:VEVENT","END:VCALENDAR",""
    ]
    return "\r\n".join(lines)

def write_row(rec: dict):
    p = _day_path(datetime.now(TZ).date())
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def save_booking(start_iso: str, name: str|None, address: str|None, caller: str|None, duration_min: int=30):
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
    ics_text = make_ics(rec["id"], start_dt, end_dt,
                        "Foreclosure Relief Consultation",
                        f"Caller: {caller or 'unknown'}; Name: {rec['name']}; Address: {rec['address']}")
    (ICS_DIR / f"{rec['id']}.ics").write_text(ics_text, encoding="utf-8")
    write_row(rec)
    return rec

def save_optout(name: str|None, address: str|None, phone: str|None):
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

# ---------- HTTP endpoints ----------
@app.post("/voice")
async def voice(_: Request):
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

@app.get("/health")
async def health():
    return JSONResponse({"ok": True})

@app.get("/")
async def index():
    return PlainTextResponse("OK")

@app.get("/ics/{bid}.ics")
async def get_ics(bid: str):
    p = ICS_DIR / f"{bid}.ics"
    if p.exists():
        return PlainTextResponse(p.read_text(encoding="utf-8"), media_type="text/calendar; charset=utf-8")
    return Response(status_code=404)

@app.get("/reports/today")
async def report_today():
    d = datetime.now(TZ).date()
    p = _day_path(d)
    if not p.exists():
        return PlainTextResponse("id,record_type,created_at,caller,name,address,appointment_start,appointment_end,note,calendar_link\n", media_type="text/csv")
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

# ---------- CR <-> OpenAI Realtime bridge (text modality) ----------
async def send_text(ws: WebSocket, token: str, last: bool):
    # Only send Twilio-CR-compliant shapes
    await ws.send_json({"type": "text", "token": token, "last": last})

TOOLS_SPEC = [
    {
        "type": "function",
        "name": "book_appointment",
        "description": "Save a consultation with name, address, and ISO8601 start time in business timezone.",
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
    await ws.accept()
    print("ConversationRelay: connected", flush=True)
    caller_number = None
    lang = "en"

    try:
        # --- Open one OpenAI Realtime session and keep the ENTIRE loop inside ---
        async with client.realtime.connect(
            model="gpt-4o-realtime"
        ) as rt:

            # Initialize session once
            instructions = system_prompt_for(lang)
            await rt.session.update(session={
                "modalities": ["text"],
                "instructions": instructions,
                "tools": TOOLS_SPEC
            })

            # Main CR <-> OpenAI loop
            while True:
                msg = await ws.receive_json()
                mtype = msg.get("type")

                if mtype == "setup":
                    caller_number = (msg.get("from") or "").strip() or None
                    continue

                if mtype == "language":
                    code = (msg.get("language") or "").lower()
                    lang = "es" if code.startswith("es") else "en"
                    instructions = system_prompt_for(lang)
                    await rt.session.update(session={"instructions": instructions})
                    continue

                if mtype == "prompt":
                    user_text = (msg.get("voicePrompt") or "").strip()
                    if not user_text:
                        continue

                    # Send the user's message to the model
                    await rt.conversation.item.create(item={
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": user_text}]
                    })
                    await rt.response.create()

                    # Stream model output back to Twilio
                    async for event in rt.stream():
                        et = event.get("type")
                        if et == "response.text.delta":
                            await send_text(ws, event.get("delta") or "", False)
                        elif et == "response.text.done":
                            await send_text(ws, "", True)
                        elif et in ("response.error", "error"):
                            # Log errors server-side only; don't send to CR
                            print("Realtime error:", event, flush=True)
                            break
                        elif et == "response.function.call":
                            fn = event.get("name")
                            args = event.get("arguments") or {}
                            tool_id = event.get("tool_call_id") or event.get("id") or uuid.uuid4().hex

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

                            await rt.response.create(
                                response={"type": "tool_result", "tool_call_id": tool_id, "output": result}
                            )
                        elif et in ("response.completed", "response.finish"):
                            break
                    continue

                if mtype == "interrupt":
                    print("Interrupted:", msg.get("utteranceUntilInterrupt", ""), flush=True)
                    # Let the model handle turn-taking; no special shape sent to CR
                    continue

                if mtype == "error":
                    print("CR error:", msg.get("description"), flush=True)
                    continue

    except WebSocketDisconnect:
        print("ConversationRelay: disconnected", flush=True)
