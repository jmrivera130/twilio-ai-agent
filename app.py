# app.py — Minimal CR ↔ OpenAI Responses relay with tool-calls + file_search (EN/ES)
# Sept 2025 pattern: Twilio ConversationRelay handles PSTN+STT+TTS; your app streams
# user turns to OpenAI Responses (with file_search + function tools). You only execute tools.
#
# Requirements (pin recent):
#   pip install "openai>=1.52.0" fastapi uvicorn
#   (Twilio CR runs over plain WebSocket; no Twilio SDK required here.)
#
# ENV you must set on Render:
#   OPENAI_API_KEY = ...
#   RELAY_WSS_URL  = wss://<your-app>.onrender.com/relay
#   TIMEZONE       = America/Los_Angeles
#   VECTOR_STORE_ID= vs_...   # your OpenAI vector store with PDFs (optional but recommended)
#   ORG_NAME       = Foreclosure Relief Group
#
# Notes:
# - We keep TwiML minimal and valid for CR; STT = Deepgram nova-3-general, EN+ES languages.
# - We DO NOT send SMS. All messages are CR WebSocket JSON (type="text").
# - Tool calls: book_appointment, mark_opt_out. We validate args, write JSONL/CSV/ICS, and respond.
# - Name loops die because the model owns slotting; we only confirm before saving.

from __future__ import annotations
import os, json, uuid, re
from datetime import datetime, timedelta, date, time
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import PlainTextResponse, JSONResponse, Response
from openai import OpenAI

# ---------- ENV ----------
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
RELAY_WSS_URL  = os.environ["RELAY_WSS_URL"]            # wss://<your-app>.onrender.com/relay
BUSINESS_TZ    = os.environ.get("TIMEZONE", "America/Los_Angeles")
VECTOR_STORE_ID= os.environ.get("VECTOR_STORE_ID", "")
ORG_NAME       = os.environ.get("ORG_NAME", "Foreclosure Relief Group")
TZ = ZoneInfo(BUSINESS_TZ)

# ---------- Storage ----------
BASE_DIR   = Path(os.environ.get("DATA_DIR", "/tmp"))
BOOK_DIR   = BASE_DIR / "appointments"; BOOK_DIR.mkdir(parents=True, exist_ok=True)
ICS_DIR    = BASE_DIR / "ics";          ICS_DIR.mkdir(parents=True, exist_ok=True)
REPORT_DIR = BASE_DIR / "reports";      REPORT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI()
client = OpenAI(api_key=OPENAI_API_KEY)

# ---------- Helpers: ICS/CSV/JSONL ----------

def _utc(dt: datetime) -> datetime:
    return dt.astimezone(ZoneInfo("UTC"))

def _ics_ts(dt: datetime) -> str:
    return _utc(dt).strftime("%Y%m%dT%H%M%SZ")

def _ics(uid: str, start_dt: datetime, end_dt: datetime, summary: str, desc: str) -> str:
    nowz = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    return "\r\n".join([
        "BEGIN:VCALENDAR","VERSION:2.0","PRODID:-//FRG//Chloe//EN","CALSCALE:GREGORIAN","METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}", f"DTSTAMP:{nowz}",
        f"DTSTART:{_ics_ts(start_dt)}", f"DTEND:{_ics_ts(end_dt)}",
        f"SUMMARY:{summary}", f"DESCRIPTION:{desc}",
        "END:VEVENT","END:VCALENDAR",""
    ])

DAYFILE = lambda d: (BOOK_DIR / f"{d.isoformat()}.jsonl")

def _write_jsonl(day: date, rec: dict):
    p = DAYFILE(day)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def save_booking(args: dict) -> dict:
    # Expected: iso_start, name, address, phone, duration_min(optional)
    sid = uuid.uuid4().hex[:12]
    start = datetime.fromisoformat(args["iso_start"]).astimezone(TZ)
    dur = int(args.get("duration_min", 30))
    end = start + timedelta(minutes=dur)
    rec = {
        "type": "booking",
        "id": sid,
        "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "caller": args.get("phone") or "",
        "name": (args.get("name") or "").strip(),
        "address": (args.get("address") or "").strip(),
        "note": args.get("note", "Consultation"),
        "calendar_link": "",
    }
    ics_text = _ics(
        sid, start, end,
        f"{ORG_NAME} Consultation",
        f"Caller: {rec['caller']}; Name: {rec['name']}; Address: {rec['address']}"
    )
    (ICS_DIR / f"{sid}.ics").write_text(ics_text, encoding="utf-8")
    _write_jsonl(start.date(), rec)
    # mirror to today for day-end CSV convenience
    try:
        mirror = dict(rec); mirror["note"] = (mirror.get("note") or "") + "; mirror=true"
        _write_jsonl(datetime.now(TZ).date(), mirror)
    except Exception:
        pass
    return rec


def save_optout(args: dict) -> dict:
    sid = uuid.uuid4().hex[:12]
    rec = {
        "type": "optout",
        "id": sid,
        "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        "start": "", "end": "",
        "caller": args.get("phone") or "",
        "name": (args.get("name") or "").strip(),
        "address": (args.get("address") or "").strip(),
        "note": "DNC request", "calendar_link": "",
    }
    _write_jsonl(datetime.now(TZ).date(), rec)
    return rec


def render_csv(d: date) -> str:
    header = [
        "id","record_type","created_at","caller","name","address",
        "appointment_start","appointment_end","note","calendar_link",
    ]
    rows = []
    p = DAYFILE(d)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                j = json.loads(line)
                rows.append([
                    j.get("id",""), j.get("type",""), j.get("created_at",""),
                    j.get("caller",""), j.get("name",""), j.get("address",""),
                    j.get("start",""), j.get("end",""), j.get("note",""), j.get("calendar_link",""),
                ])
            except Exception:
                continue
    out = [",".join(header)]
    for r in rows:
        safe = ['"{}"'.format(str(x).replace('"','""')) for x in r]
        out.append(",".join(safe))
    return "\n".join(out)

# ---------- HTTP: TwiML + reports ----------
@app.post("/voice")
async def voice(_: Request):
    # Minimal, current CR TwiML with Deepgram nova-3 and EN/ES language entries.
    twiml = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<Response>
  <Connect>
    <ConversationRelay
      url=\"{RELAY_WSS_URL}\"
      transcriptionProvider=\"Deepgram\"
      speechModel=\"nova-3-general\"
      ttsProvider=\"Amazon\">
      <Language code=\"en-US\" voice=\"Joanna-Neural\" />
      <Language code=\"es-US\" voice=\"Lupe-Neural\" />
    </ConversationRelay>
  </Connect>
</Response>"""
    return PlainTextResponse(twiml, media_type="text/xml")

@app.get("/")
async def index():
    return PlainTextResponse("OK")

@app.get("/health")
async def health():
    return JSONResponse({"ok": True})

@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)

@app.get("/ics/{bid}.ics")
async def get_ics(bid: str):
    p = ICS_DIR / f"{bid}.ics"
    if p.exists():
        return PlainTextResponse(p.read_text(encoding="utf-8"), media_type="text/calendar; charset=utf-8")
    return Response(status_code=404)

@app.get("/reports/{day}")
async def report_day(day: str):
    try:
        d = datetime.fromisoformat(day).date()
    except Exception:
        return JSONResponse({"error": "bad date"}, status_code=400)
    csv_text = render_csv(d)
    fname = f"appointments-{d.isoformat()}.csv"
    return PlainTextResponse(csv_text, media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=\"{fname}\""})

# ---------- OpenAI glue ----------
SYSTEM_EN = (
    "You are Chloe from " + ORG_NAME + ". Be warm and concise (≤2 short sentences). "
    "Keep the entire call in the caller’s chosen language (English or Spanish). "
    "Use file_search on the attached documents to answer questions accurately; cite briefly when helpful. "
    "Only propose scheduling after the caller asks for next steps, asks to speak to a person, or confirms they want an appointment. "
    "Before calling any tool, summarize the details you heard in ONE sentence and ask for a clear yes/no. "
    "If the caller changes topic mid-flow, gracefully pivot—do NOT repeat prior prompts. "
    "Never ask for the same field more than twice; if unclear, acknowledge and move on to clarify later."
)

SYSTEM_ES = (
    "Eres Chloe de " + ORG_NAME + ". Sé cálida y concisa (≤2 frases). "
    "Mantén toda la llamada en el idioma elegido (inglés o español). "
    "Usa file_search en los documentos adjuntos para responder con precisión; incluye una cita breve cuando ayude. "
    "Propón agendar solo cuando la persona pida próximos pasos, quiera hablar con alguien o confirme que desea una cita. "
    "Antes de usar cualquier herramienta, resume los datos en UNA frase y pide un sí/no claro. "
    "Si la persona cambia de tema, cambia con naturalidad—NO repitas solicitudes previas. "
    "Nunca pidas el mismo dato más de dos veces; si no está claro, reconoce y avanza para aclararlo luego."
)

TOOLS = [
    {"type": "file_search"},
    {
        "type": "function",
        "name": "book_appointment",
        "description": "Book a consultation appointment.",
        "parameters": {
            "type": "object",
            "properties": {
                "iso_start": {"type": "string", "description": "Start datetime in ISO 8601 with timezone."},
                "name": {"type": "string"},
                "address": {"type": "string"},
                "phone": {"type": "string"},
                "duration_min": {"type": "integer", "default": 30}
            },
            "required": ["iso_start", "name", "address"]
        }
    },
    {
        "type": "function",
        "name": "mark_opt_out",
        "description": "Mark the caller as do-not-contact.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "address": {"type": "string"},
                "phone": {"type": "string"}
            },
            "required": ["name"]
        }
    }
]

LANG_HINT_RE = re.compile(r"\b(espanol|español|spanish|ingl[eé]s|english)\b", re.I)

async def send_text(ws: WebSocket, text: str):
    # Twilio CR expects this JSON shape; see 64107 if you deviate.
    await ws.send_json({"type": "text", "token": text, "last": True})

# Safely pull tool calls from a Responses object regardless of SDK minor changes.

def extract_tool_calls(resp_obj) -> list[dict]:
    try:
        d = resp_obj.to_dict() if hasattr(resp_obj, "to_dict") else resp_obj
    except Exception:
        d = resp_obj
    calls = []
    # Look through "output" list for items with "type":"tool_call"
    for item in (d.get("output") or []):
        if item.get("type") == "tool_call":
            tc = item.get("tool_call") or {}
            calls.append(tc)
    # Some SDKs put tool calls under output[...].content[...]
    if not calls:
        for item in (d.get("output") or []):
            for c in (item.get("content") or []):
                if isinstance(c, dict) and c.get("type") == "tool_call":
                    calls.append(c.get("tool_call") or {})
    return calls


def output_text(resp_obj) -> str:
    try:
        return (resp_obj.output_text or "").strip()
    except Exception:
        # Fallback: concatenate text content from output
        d = resp_obj if isinstance(resp_obj, dict) else getattr(resp_obj, "to_dict", lambda: {})()
        parts = []
        for item in (d.get("output") or []):
            for c in (item.get("content") or []):
                if isinstance(c, dict) and c.get("type") == "output_text":
                    parts.append(c.get("text") or "")
        return " ".join(parts).strip()

# ---------- WebSocket: Twilio connects here ----------
@app.websocket("/relay")
async def relay(ws: WebSocket):
    await ws.accept()
    print("ConversationRelay: connected", flush=True)
        # Running conversation state for this WS session
    history: list[dict] = []


    # Minimal local state (the model leads the dialog; we just switch language and execute tools)
    caller_number = None
    chosen_lang = None  # "en-US" or "es-US"

    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")

            if mtype == "setup":
                caller_number = (msg.get("from") or "").strip() or None
                continue

            if mtype == "prompt":
                utter = (msg.get("voicePrompt") or "").strip()
                if not utter:
                    continue
                print("RX:", utter, flush=True)

                # Language switch (local—also tell CR to use matching <Language>)
                if LANG_HINT_RE.search(utter):
                    if re.search(r"espanol|español|spanish", utter, re.I):
                        chosen_lang = "es-US"
                        await ws.send_json({"type":"language","transcriptionLanguage":"es-US","ttsLanguage":"es-US"})
                        await send_text(ws, "Entendido. Puedo ayudarte en español.")
                        continue
                    if re.search(r"ingl[eé]s|english", utter, re.I):
                        chosen_lang = "en-US"
                        await ws.send_json({"type":"language","transcriptionLanguage":"en-US","ttsLanguage":"en-US"})
                        await send_text(ws, "Got it. I’ll continue in English.")
                        continue

                system = SYSTEM_ES if chosen_lang == "es-US" else SYSTEM_EN

                # Compose the Responses call
                extra_body = {}
                if VECTOR_STORE_ID:
                    extra_body = {"attachments": [{"vector_store_id": VECTOR_STORE_ID}]}

                try:
                    # Append new user turn to history BEFORE calling the model
                    history.append({"role": "user", "content": utter})

                    resp = client.responses.create(
                        model="gpt-4o-mini",
                        input=[{"role": "system", "content": system}, *history[-20:]],  # keep last ~20 turns
                        tools=TOOLS,
                        max_output_tokens=220,
                        temperature=0.3,
                        extra_body=({"attachments": [{"vector_store_id": VECTOR_STORE_ID}]} if VECTOR_STORE_ID else None),
                    )

                except Exception as e:
                    print("OpenAI error:", repr(e), flush=True)
                    await send_text(ws, "I’m having trouble right now. Could you please repeat?")
                    continue

                # Execute tool calls if any
                calls = extract_tool_calls(resp)
                if calls:
                    for tc in calls:
                        name = tc.get("name")
                        try:
                            args = json.loads(tc.get("arguments") or "{}")
                        except Exception:
                            args = {}
                        result = {}
                        if name == "book_appointment":
                            # Ensure caller phone if missing
                            if caller_number and not args.get("phone"):
                                args["phone"] = caller_number
                            try:
                                out = save_booking(args)
                                result = {"ok": True, "id": out["id"], "start": out["start"], "end": out["end"]}
                            except Exception as e:
                                result = {"ok": False, "error": f"booking_failed: {e}"}
                        elif name == "mark_opt_out":
                            if caller_number and not args.get("phone"):
                                args["phone"] = caller_number
                            try:
                                out = save_optout(args)
                                result = {"ok": True, "id": out["id"]}
                            except Exception as e:
                                result = {"ok": False, "error": f"optout_failed: {e}"}
                        else:
                            result = {"ok": False, "error": f"unknown_tool: {name}"}

                        # 1) Record the assistant's tool call in history so the model can follow up coherently
                        tool_name = name
                        tool_id = tc.get("id") or uuid.uuid4().hex  # tolerate missing id

                        history.append({
                            "role": "assistant",
                            "content": [{
                                "type": "tool_call",
                                "id": tool_id,
                                "name": tool_name,
                                "arguments": json.dumps(args, ensure_ascii=False)
                            }]
                        })

                        # 2) Record the tool result turn
                        history.append({
                            "role": "tool",
                            "content": json.dumps(result, ensure_ascii=False),
                            "name": tool_name,
                            "tool_call_id": tool_id
                        })

                        # 3) Ask the model for the final confirmation/message with full history
                        follow = client.responses.create(
                            model="gpt-4o-mini",
                            input=[{"role":"system","content": system}, *history[-20:]],
                            max_output_tokens=160,
                            temperature=0.2,
                            tools=TOOLS,  # keep tools attached in case the model wants to chain
                            extra_body=({"attachments": [{"vector_store_id": VECTOR_STORE_ID}]} if VECTOR_STORE_ID else None),
                        )
                        final_text = output_text(follow) or "Done."
                        history.append({"role": "assistant", "content": final_text})
                        await send_text(ws, final_text)

                    continue

                # No tools—send the model’s direct answer
                text = output_text(resp) or "Sorry, could you say that again?"
                history.append({"role": "assistant", "content": text})
                await send_text(ws, text)
                continue

            if mtype == "interrupt":
                # Log and gently reset: add a brief assistant ack so the model understands we paused
                cut = (msg.get("utteranceUntilInterrupt") or "").strip()
                print("Interrupted:", cut, flush=True)
                # Optional: mark in history for clarity (developer/meta role not always supported; use assistant)
                history.append({"role": "assistant", "content": "Understood—go ahead."})
                continue

            if mtype == "error":
                print("ConversationRelay error:", msg.get("description"), flush=True)
                continue

    except WebSocketDisconnect:
        print("ConversationRelay: disconnected", flush=True)
