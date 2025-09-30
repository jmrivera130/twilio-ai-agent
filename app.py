import logging, time, traceback, os, json
LOG_LEVEL = os.getenv('LOG_LEVEL','INFO').upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format='%(message)s')
class JsonLogger:
    def __init__(self, name='app'):
        self.log = logging.getLogger(name)
    def _emit(self, level, event, **kw):
        payload = {'level': level, 'event': event, 'ts': time.time()}
        payload.update({k:v for k,v in kw.items() if v is not None})
        self.log.log(getattr(logging, level, logging.INFO), json.dumps(payload, ensure_ascii=False))
    def info(self, event, **kw): self._emit('INFO', event, **kw)
    def warn(self, event, **kw): self._emit('WARNING', event, **kw)
    def error(self, event, **kw): self._emit('ERROR', event, **kw)
jsonlog = JsonLogger('chloe')
from contextlib import contextmanager
@contextmanager
def section(name, **fields):
    t0 = time.time()
    jsonlog.info('section.start', name=name, **fields)
    try:
        yield
        jsonlog.info('section.ok', name=name, ms=int((time.time()-t0)*1000))
    except Exception as e:
        tb = traceback.format_exc()
        jsonlog.error('section.fail', name=name, error=str(e), traceback=tb, ms=int((time.time()-t0)*1000))
        raise

"""
Modern Twilio ConversationRelay → OpenAI Responses agent.

This script uses the asynchronous OpenAI client (AsyncOpenAI) to stream
responses and handle function calls from the OpenAI Responses API. It
supports English and Spanish, integrates a vector store for document
retrieval via the `file_search` tool, and exposes two custom tools to
book appointments and mark do‑not‑contact requests. A strict booking
guard prevents accidental bookings based on a bare "yes" or "ok".

Environment variables:
  OPENAI_API_KEY   – your OpenAI API key (required)
  RELAY_WSS_URL    – ws URL for Twilio ConversationRelay (e.g. wss://<domain>/relay)
  TIMEZONE         – IANA timezone for your business (default America/Los_Angeles)
  VECTOR_STORE_ID  – ID of your OpenAI vector store (optional)
  ORG_NAME         – your organization’s name (default "Foreclosure Relief Group")
  APP_VERSION      – version string shown at startup (optional)

To run locally:
  pip install "openai>=1.52.0" fastapi uvicorn
  uvicorn app:app --host 0.0.0.0 --port 8000

On Render, ensure the above env vars are set and Twilio Voice webhook
points to POST /voice on your deployed domain.

"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import PlainTextResponse, JSONResponse, Response
from openai import AsyncOpenAI

# ---------------------------------------------------------------------------
# Environment configuration
# ---------------------------------------------------------------------------

APP_VERSION = os.environ.get("APP_VERSION", "local")
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
RELAY_WSS_URL = os.environ["RELAY_WSS_URL"]
BUSINESS_TZ = os.environ.get("TIMEZONE", "America/Los_Angeles")
# Use separate vector store IDs for call scripts and policies.  If either is
# empty, the file_search tool will be omitted for that call.  To retain
# backwards‑compatibility with a single store setup, fall back to
# VECTOR_STORE_ID if VECTOR_STORE_CALLSCRIPTS_ID is not provided.
VECTOR_STORE_CALLSCRIPTS_ID = (
    os.environ.get("VECTOR_STORE_CALLSCRIPTS_ID")
    or os.environ.get("VECTOR_STORE_ID")
    or ""
).strip()
VECTOR_STORE_POLICIES_ID = os.environ.get("VECTOR_STORE_POLICIES_ID", "").strip()
ORG_NAME = os.environ.get("ORG_NAME", "Foreclosure Relief Group")

# Print startup info and a build marker for debugging deployments.
print("=== NEW BUILD LOADED ===", flush=True)
print(f"APP_VERSION={APP_VERSION}  RELAY_WSS_URL={RELAY_WSS_URL}  TZ={BUSINESS_TZ}", flush=True)

TZ = ZoneInfo(BUSINESS_TZ)

# ---------------------------------------------------------------------------
# Storage directories
# ---------------------------------------------------------------------------

BASE_DIR = Path(os.environ.get("DATA_DIR", "/tmp"))
BOOK_DIR = BASE_DIR / "appointments"; BOOK_DIR.mkdir(parents=True, exist_ok=True)
ICS_DIR = BASE_DIR / "ics";          ICS_DIR.mkdir(parents=True, exist_ok=True)
REPORT_DIR = BASE_DIR / "reports";    REPORT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# FastAPI application and OpenAI client
# ---------------------------------------------------------------------------

app = FastAPI()
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ---------------------------------------------------------------------------
# Helper functions for appointment booking and opt‑out
# ---------------------------------------------------------------------------

def _utc(dt: datetime) -> datetime:
    """Convert datetime to UTC."""
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

def _write_jsonl(day: date, rec: dict) -> None:
    p = DAYFILE(day)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def save_booking(args: dict) -> dict:
    """Persist a new booking and write an .ics file and CSV/JSONL entries."""
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
    # Mirror today’s record for reporting across date boundaries
    try:
        mirror = dict(rec)
        mirror["note"] = (mirror.get("note") or "") + "; mirror=true"
        _write_jsonl(datetime.now(TZ).date(), mirror)
    except Exception:
        pass
    return rec

def save_optout(args: dict) -> dict:
    """Persist a do‑not‑contact request."""
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
    rows: list[list[str]] = []
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

# ---------------------------------------------------------------------------
# Twilio HTTP endpoints
# ---------------------------------------------------------------------------

@app.post("/voice")
async def voice(_: Request) -> PlainTextResponse:
    """Return minimal TwiML to start a ConversationRelay session."""
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
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
async def index() -> PlainTextResponse:
    return PlainTextResponse("OK")

@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True})

@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(status_code=204)

@app.get("/ics/{bid}.ics")
async def get_ics(bid: str) -> Response:
    p = ICS_DIR / f"{bid}.ics"
    if p.exists():
        return PlainTextResponse(p.read_text(encoding="utf-8"), media_type="text/calendar; charset=utf-8")
    return Response(status_code=404)

@app.get("/reports/{day}")
async def report_day(day: str) -> Response:
    try:
        d = datetime.fromisoformat(day).date()
    except Exception:
        return JSONResponse({"error": "bad date"}, status_code=400)
    csv_text = render_csv(d)
    fname = f"appointments-{d.isoformat()}.csv"
    return PlainTextResponse(
        csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=\"{fname}\""},
    )

# ---------------------------------------------------------------------------
# System prompts and tools
# ---------------------------------------------------------------------------

SYSTEM_EN = (
    # Introduction and tone
    "You are Chloe from " + ORG_NAME + ", a real human advisor helping homeowners facing foreclosure. "
    "On the first interaction with a caller, greet them warmly, introduce yourself and " + ORG_NAME + ", "
    "and briefly explain that you assist people exploring options to avoid foreclosure. "
    "Politely ask whether they prefer to continue in English or Spanish, and keep the call in their chosen language. "
    # Empathy and brevity
    "Throughout the call, be empathetic and concise—keep replies to no more than two short sentences. "
    "Internally, use the file_search tool to retrieve information when needed, but never mention documents, PDFs, uploads, tools, or vector stores to the caller. "
    "Summarize information gently; if multiple alternatives exist, offer to discuss them one at a time or to schedule a consultation. "
    # Scheduling guidance
    "Only propose scheduling an appointment after the caller asks for next steps, expresses a desire to speak with someone, or confirms they want an appointment. "
    "Before calling any tool, summarize the details you heard in one sentence and ask for a clear yes/no. "
    "When booking an appointment, collect information step by step: first ask for the desired date and time, then confirm the name and the property address. Do not ask for all details at once. "
    # Flow and interruptions
    "If the caller changes topic mid‑flow, pivot gracefully—do NOT repeat prior prompts. "
    "Never ask for the same field more than twice; if unclear, acknowledge and move on to clarify later. "
    "If the caller interrupts while you are speaking, stop immediately and acknowledge their input in one sentence before continuing. "
    # Error handling
    "If you encounter an error or cannot retrieve the requested information, apologize briefly and ask them to repeat or clarify. "
)

SYSTEM_ES = (
    # Presentación y tono
    "Eres Chloe de " + ORG_NAME + ", una asesora real que ayuda a propietarios que enfrentan la ejecución hipotecaria. "
    "En la primera interacción con la persona, salúdala con calidez, preséntate y presenta a " + ORG_NAME + ", "
    "y explica brevemente que ayudas a explorar opciones para evitar la ejecución. "
    "Pregunta amablemente si prefiere continuar en inglés o español y mantén la llamada en el idioma elegido. "
    # Empatía y brevedad
    "A lo largo de la llamada, sé empática y concisa; no uses más de dos frases cortas por respuesta. "
    "Internamente, utiliza la herramienta file_search para recuperar información cuando sea necesario, pero nunca menciones documentos, archivos PDF, cargas, herramientas ni almacenes vectoriales a la persona. "
    "Resume la información con suavidad; si existen varias alternativas, ofrécele tratarlas una por una o programar una consulta. "
    # Guía para agendar
    "Propón agendar una cita solo cuando la persona pida los siguientes pasos, exprese deseo de hablar con alguien o confirme que quiere una cita. "
    "Antes de usar cualquier herramienta, resume los datos en una frase y pide un sí/no claro. "
    "Al programar una cita, recopila la información paso a paso: primero pregunta la fecha y hora deseadas, luego confirma el nombre y la dirección de la propiedad. No pidas todos los datos de una sola vez. "
    # Flujo e interrupciones
    "Si la persona cambia de tema, adapta la conversación de manera natural—NO repitas solicitudes previas. "
    "Nunca pidas el mismo dato más de dos veces; si no está claro, reconoce y continúa para aclararlo luego. "
    "Si la persona te interrumpe mientras hablas, detente de inmediato y reconoce su comentario en una frase antes de continuar. "
    # Manejo de errores
    "Si encuentras un error o no puedes obtener la información solicitada, discúlpate brevemente y pide que repita o aclare. "
)

# Regular expressions for booking guard and language hints remain above.

# Detect factual questions that should trigger the Policies vector store.
POLICY_QUESTION_RE = re.compile(
    r"\b(what|why|how|que|qué|porque|por\s+qué|como|cómo)\b", re.I
)

# Base function tools definitions.  These remain constant for each call and are
# appended to the dynamic tools list built per user utterance.
# Define function tools for the Responses API.  Each entry must include
# a top‑level "name", "description", and "parameters".  The newer nested
# "function" schema does not apply to Responses as of Sept 2025—using it
# causes missing parameter errors.  See OpenAI's file search and tool docs
# (Sept 2025) for details【552313873166555†screenshot】.
# Define function tools using the nested `function` schema.  For the
# OpenAI Responses API (Sept 2025), each function tool must be represented
# with a top‑level `type: "function"` and a nested `function` key
# containing the name, description and parameters.  Failing to nest
# the spec will result in an error about a missing `name` on the tool
# entry (e.g. “tools[1].name” missing).  See the latest docs【552313873166555†screenshot】.
FUNCTION_TOOLS: list[dict[str, object]] = [
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": "Book a consultation appointment.",
            "parameters": {
                "type": "object",
                "properties": {
                    "iso_start": {
                        "type": "string",
                        "description": "Start datetime in ISO 8601 with timezone."
                    },
                    "name": {"type": "string"},
                    "address": {"type": "string"},
                    "phone": {"type": "string"},
                    "duration_min": {
                        "type": "integer",
                        "default": 30
                    },
                    "note": {"type": "string"},
                },
                "required": ["iso_start", "name", "address"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_opt_out",
            "description": "Mark the caller as do‑not‑contact.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "address": {"type": "string"},
                    "phone": {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },
]

def choose_vector_store(user_text: str) -> str:
    """Select the appropriate vector store ID based on the caller's message.

    If the message appears to be a factual question (starts with what/why/how/etc.),
    return the Policies store ID; otherwise return the CallScripts store ID. If
    neither is configured, return an empty string.
    """
    if POLICY_QUESTION_RE.search(user_text or ""):
        return VECTOR_STORE_POLICIES_ID or VECTOR_STORE_CALLSCRIPTS_ID
    return VECTOR_STORE_CALLSCRIPTS_ID or VECTOR_STORE_POLICIES_ID

def build_tools_for_user(user_text: str) -> list[dict]:
    ids = [i for i in [VECTOR_STORE_CALLSCRIPTS_ID, VECTOR_STORE_POLICIES_ID] if i]
    tools: list[dict] = []
    if ids:
        tools.append({'type': 'file_search', 'vector_store_ids': ids})
    function_tools = _sanitize_function_tools(FUNCTION_TOOLS)
    tools.extend(function_tools)
    jsonlog.info('tools.built', tools_preview=[{'type':t.get('type'),'name':t.get('name')} for t in tools])
    _validate_tools(tools)
    return tools

# Regular expressions for booking guard and language hints
ASK_SCHED_EN = re.compile(r"\b(would you like to|shall we|do you want to) (schedule|book).+\?", re.I)
ASK_SCHED_ES = re.compile(r"\b(quieres|deseas) (agendar|programar|concertar).+\?", re.I)
LANG_HINT_RE = re.compile(r"\b(espanol|español|spanish|ingl[eé]s|english)\b", re.I)
SCHED_RE = re.compile(r"\b(book|schedule|appointment|set\s*up|consult|cita|agendar|programar)\b", re.I)

async def send_text(ws: WebSocket, text: str) -> None:
    """Send a complete utterance to Twilio CR."""
    await ws.send_json({"type": "text", "token": text, "last": True})

async def cr_send(ws: WebSocket, token: str, last: bool = False) -> None:
    """Send a token frame to Twilio CR."""
    await ws.send_json({"type": "text", "token": token, "last": last})


def invites_booking_now(assistant_text: str, lang: str | None) -> bool:
    """Return True if assistant_text contains a booking invite in the given language."""
    if not assistant_text:
        return False
    if lang == "es-US":
        return bool(ASK_SCHED_ES.search(assistant_text))
    return bool(ASK_SCHED_EN.search(assistant_text))

def extract_tool_calls(resp_obj) -> list[dict]:
    """Extract tool_call objects from a Responses API response."""
    try:
        d = resp_obj.to_dict() if hasattr(resp_obj, "to_dict") else resp_obj
    except Exception:
        d = resp_obj
    calls: list[dict] = []
    for item in (d.get("output") or []):
        if item.get("type") == "tool_call":
            tc = item.get("tool_call") or {}
            calls.append(tc)
    if not calls:
        for item in (d.get("output") or []):
            for c in (item.get("content") or []):
                if isinstance(c, dict) and c.get("type") == "tool_call":
                    calls.append(c.get("tool_call") or {})
    return calls

def output_text(resp_obj) -> str:
    """Extract concatenated assistant text from a Responses API response."""
    try:
        return (resp_obj.output_text or "").strip()
    except Exception:
        d = resp_obj if isinstance(resp_obj, dict) else getattr(resp_obj, "to_dict", lambda: {})()
        parts: list[str] = []
        for item in (d.get("output") or []):
            for c in (item.get("content") or []):
                if isinstance(c, dict) and c.get("type") == "output_text":
                    parts.append(c.get("text") or "")
        return " ".join(parts).strip()

# ---------------------------------------------------------------------------
# WebSocket endpoint for Twilio ConversationRelay
# ---------------------------------------------------------------------------

@app.websocket("/relay")
async def relay(ws: WebSocket) -> None:
    await ws.accept()
    print("ConversationRelay: connected", flush=True)

    # Per‑call state
    history: list[dict] = []
    caller_number: str | None = None
    chosen_lang: str | None = None  # "en-US" or "es-US"

    # Booking guard state: these flags track whether we've offered a booking invite
    offered_booking = False
    offered_token_id: str | None = None

    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")

            with section('ws.setup'):
                if mtype == "setup":
                caller_number = (msg.get("from") or "").strip() or None
                # Instant greeting to hide model cold-start and offer language choice
                await cr_send(ws, f"Hi, this is Chloe with {ORG_NAME}. ")
                await cr_send(ws, "Would you like to continue in English or Spanish?", last=True)
                continue

            if mtype == "prompt":
                user_text = (msg.get("voicePrompt") or "").strip()
                if not user_text:
                    continue
                print("RX:", user_text, flush=True)

                # Handle language hints and mid‑call language switching
                if LANG_HINT_RE.search(user_text):
                    if re.search(r"espanol|español|spanish", user_text, re.I):
                        chosen_lang = "es-US"
                        await ws.send_json({"type": "language", "transcriptionLanguage": "es-US", "ttsLanguage": "es-US"})
                        await send_text(ws, "Entendido. Puedo ayudarte en español.")
                        offered_booking = False; offered_token_id = None
                        continue
                    if re.search(r"ingl[eé]s|english", user_text, re.I):
                        chosen_lang = "en-US"
                        await ws.send_json({"type": "language", "transcriptionLanguage": "en-US", "ttsLanguage": "en-US"})
                        await send_text(ws, "Got it. I’ll continue in English.")
                        offered_booking = False; offered_token_id = None
                        continue

                # Determine which system prompt to use
                system = SYSTEM_ES if chosen_lang == "es-US" else SYSTEM_EN

                # Append the user message to history and call the model
                history.append({"role": "user", "content": user_text})
                try:
                    response = await jsonlog.info('openai.call.begin');
            with section('openai.responses.create'):
                tools = _harden_schemas(tools)
jsonlog.info('tools.final', tools=tools)
responses_create_normalized(client, 
                        model="gpt-4o-mini",
                        input=[
                            {"role": "system", "content": system},
                            *history[-8:],
                        ],
                        tools=build_tools_for_user(user_text),
                        max_output_tokens=220,
                        temperature=0.3,
                    )
                except Exception as e:
                    print("OpenAI error:", repr(e), flush=True)
                    await send_text(ws, "Sorry, I had a problem—could you say that again?")
                    continue

                # If the model calls a tool, execute it
                tool_calls = extract_tool_calls(response)
                if tool_calls:
                    for tc in tool_calls:
                        name = (tc.get("name") or "").strip()
                        try:
                            args = json.loads(tc.get("arguments") or "{}")
                        except Exception:
                            args = {}

                        # Strict booking guard: only allow booking if we invited, or user asked to schedule, or we have a full iso_start
                        if name == "book_appointment":
                            explicit_user_intent = bool(SCHED_RE.search(user_text))
                            allow = (offered_booking is True) or explicit_user_intent or bool(args.get("iso_start"))
                            if not allow:
                                result = {"ok": False, "error": "guard_blocked_need_explicit_intent"}
                            else:
                                if caller_number and not args.get("phone"):
                                    args["phone"] = caller_number
                                try:
                                    out = save_booking(args)
                                    result = {"ok": True, "id": out["id"], "start": out["start"], "end": out["end"]}
                                except Exception as e:
                                    result = {"ok": False, "error": f"booking_failed: {e}"}
                            offered_booking = False; offered_token_id = None

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

                        # Append the tool call and its result to the history
                        tool_id = tc.get("id") or uuid.uuid4().hex
                        history.append({
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_call",
                                    "id": tool_id,
                                    "name": name,
                                    "arguments": json.dumps(args, ensure_ascii=False),
                                }
                            ],
                        })
                        history.append({
                            "role": "tool",
                            "content": json.dumps(result, ensure_ascii=False),
                            "name": name,
                            "tool_call_id": tool_id,
                        })

                        # Ask the model to generate a closing statement after executing the tool
                        follow = await jsonlog.info('openai.call.begin');
            with section('openai.responses.create'):
                tools = _harden_schemas(tools)
jsonlog.info('tools.final', tools=tools)
responses_create_normalized(client, 
                            model="gpt-4o-mini",
                            input=[{"role": "system", "content": system}, *history[-24:]],
                            tools=build_tools_for_user(""),
                            max_output_tokens=180,
                            temperature=0.2,
                        )
                        final_text = output_text(follow) or "Done."
                        history.append({"role": "assistant", "content": final_text})

                        # Update the booking guard based on the new assistant text
                        offered_booking = invites_booking_now(final_text, chosen_lang)
                        offered_token_id = uuid.uuid4().hex if offered_booking else None

                        await send_text(ws, final_text)
                    continue  # after processing all tool calls

                # No tools called; send the model’s direct answer
                text = output_text(response) or "Could you say that again?"
                history.append({"role": "assistant", "content": text})
                offered_booking = invites_booking_now(text, chosen_lang)
                offered_token_id = uuid.uuid4().hex if offered_booking else None
                await send_text(ws, text)
                continue

            if mtype == "interrupt":
                # The caller barge‑in; record a neutral acknowledgement and reset any pending invite
                cut = (msg.get("utteranceUntilInterrupt") or "").strip()
                print("Interrupted:", cut, flush=True)
                history.append({"role": "assistant", "content": "Understood—go ahead."})
                offered_booking = False; offered_token_id = None
                continue

    except WebSocketDisconnect:
        print("ConversationRelay: disconnected", flush=True)
    except Exception as e:
        print("WebSocket error:", repr(e), flush=True)
    finally:
        try:
            await ws.close()
        except Exception:
            pass
def _sanitize_function_tools(raw_tools: list[dict]) -> list[dict]:
    out = []
    for i, t in enumerate(raw_tools or []):
        if not isinstance(t, dict):
            raise ValueError(f'function tool at index {i} is not a dict')
        # Accept already-flat entries
        if t.get('type') == 'function' and 'name' in t:
            out.append(t)
            continue
        # Convert nested assistants-style to flat responses-style
        if t.get('type') == 'function' and isinstance(t.get('function'), dict):
            fn = t['function']
            if 'name' not in fn:
                raise ValueError(f"function tool at index {i} missing 'name'")
            flat = {'type':'function'}
            flat.update({k:v for k,v in fn.items()})
            out.append(flat)
            continue
        # Convert bare function spec
        if 'name' in t and 'parameters' in t:
            out.append({'type':'function', **t})
            continue
        raise ValueError(f'invalid function tool at index {i}: {t}')
    return out

def _validate_tools(tools: list[dict]) -> None:
    if not isinstance(tools, list):
        raise ValueError('tools must be a list')
    for idx, item in enumerate(tools):
        if item.get('type') == 'function':
            if 'name' not in item:
                raise ValueError(f"tools[{idx}].name missing (flattened schema)")
            if 'parameters' not in item:
                raise ValueError(f"tools[{idx}].parameters missing")
        elif item.get('type') == 'file_search':
            ids = item.get('vector_store_ids') or []
            if not ids:
                raise ValueError('file_search tool missing vector_store_ids')
    jsonlog.info('tools.validated', count=len(tools))

def _harden_schemas(tools: list[dict]) -> list[dict]:
    for t in tools:
        if t.get('type') == 'function':
            params = t.get('parameters')
            if isinstance(params, dict) and 'additionalProperties' not in params:
                params['additionalProperties'] = False
    return tools


def _ensure_responses_tools_kw(**kwargs):
    t = kwargs.get("tools")
    if t is None:
        return kwargs
    # Flatten function tools to Responses-style: {'type':'function','name':..., 'parameters': {...}}
    flat = []
    for i, item in enumerate(t):
        if not isinstance(item, dict):
            raise ValueError(f"tools[{i}] must be dict, got {type(item)}")
        if item.get("type") == "function":
            if "name" in item:
                flat.append(item)  # already flat
            elif isinstance(item.get("function"), dict):
                fn = item["function"]
                if "name" not in fn:
                    raise ValueError(f"tools[{i}].name missing")
                merged = {"type": "function"}
                merged.update(fn)
                flat.append(merged)
            else:
                raise ValueError(f"invalid function tool at {i}: {item}")
        elif item.get("type") == "file_search":
            ids = item.get("vector_store_ids") or []
            if not ids:
                raise ValueError("file_search tool missing vector_store_ids")
            flat.append(item)
        else:
            flat.append(item)
    kwargs["tools"] = flat
    return kwargs

def responses_create_normalized(client, **kwargs):
    kwargs = _ensure_responses_tools_kw(**kwargs)
    jsonlog.info("tools.final", tools=kwargs.get("tools"))
    return responses_create_normalized(client, **kwargs)
