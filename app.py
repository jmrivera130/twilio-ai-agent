
from __future__ import annotations

import os, json, re, time, traceback, uuid
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo
import logging
from contextlib import contextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import PlainTextResponse, JSONResponse, Response
from openai import AsyncOpenAI

# ---------------- logging ----------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(message)s")

class JsonLogger:
    def __init__(self, name="app"):
        self.log = logging.getLogger(name)
    def _emit(self, level, event, **kw):
        payload = {"level": level, "event": event, "ts": time.time()}
        payload.update({k: v for k, v in kw.items() if v is not None})
        self.log.log(getattr(logging, level, logging.INFO), json.dumps(payload, ensure_ascii=False))
    def info(self, event, **kw): self._emit("INFO", event, **kw)
    def warn(self, event, **kw): self._emit("WARNING", event, **kw)
    def error(self, event, **kw): self._emit("ERROR", event, **kw)

jsonlog = JsonLogger("chloe")

@contextmanager
def section(name, **fields):
    t0 = time.time()
    jsonlog.info("section.start", name=name, **fields)
    try:
        yield
        jsonlog.info("section.ok", name=name, ms=int((time.time()-t0)*1000))
    except Exception as e:
        jsonlog.error("section.fail", name=name, error=str(e), traceback=traceback.format_exc(), ms=int((time.time()-t0)*1000))
        raise

# ---------------- env ----------------
APP_VERSION = os.environ.get("APP_VERSION", "local")
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
GIT_COMMIT = os.environ.get("GIT_COMMIT", "dev")
RELAY_WSS_URL = os.environ["RELAY_WSS_URL"]
ORG_NAME = os.environ.get("ORG_NAME", "Foreclosure Relief Group")
BUSINESS_TZ = os.environ.get("TIMEZONE", "America/Los_Angeles")

VECTOR_STORE_CALLSCRIPTS_ID = (os.environ.get("VECTOR_STORE_CALLSCRIPTS_ID") or os.environ.get("VECTOR_STORE_ID") or "").strip()
VECTOR_STORE_POLICIES_ID = (os.environ.get("VECTOR_STORE_POLICIES_ID") or "").strip()

print("=== NEW BUILD LOADED ===", flush=True)
print(f"APP_VERSION={APP_VERSION}  RELAY_WSS_URL={RELAY_WSS_URL}  TZ={BUSINESS_TZ}  GIT_COMMIT={GIT_COMMIT}", flush=True)

TZ = ZoneInfo(BUSINESS_TZ)

# ---------------- storage ----------------
BASE_DIR = Path(os.environ.get("DATA_DIR", "/tmp"))
BOOK_DIR = BASE_DIR / "appointments"; BOOK_DIR.mkdir(parents=True, exist_ok=True)
ICS_DIR = BASE_DIR / "ics"; ICS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------- app & client ----------------
app = FastAPI()
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ---------------- redaction ----------------
REDACT_PATTERNS = [r"(?i)\b(files?|uploads?|tools?|vector stores?|RAG)\b"]
def redact_output(text: str) -> str:
    if not text: return text
    out = text
    for pat in REDACT_PATTERNS:
        out = re.sub(pat, "internal info", out)
    return out.strip()

# ---------------- tool execution & state ----------------
def _safe_get(d, *keys, default=None):
    cur = d
    for k in keys:
        if cur is None: return default
        if isinstance(cur, dict):
            cur = cur.get(k, None)
        else:
            cur = getattr(cur, k, None)
    return cur if cur is not None else default

def extract_tool_uses(resp) -> list[dict]:
    uses = []
    out = _safe_get(resp, "output", default=None)
    if out and isinstance(out, (list, tuple)):
        for item in out:
            content = _safe_get(item, "content", default=[])
            if isinstance(content, (list, tuple)):
                for c in content:
                    c_type = _safe_get(c, "type", default=None) or _safe_get(c, "item", default=None)
                    name = _safe_get(c, "name", default=None)
                    args = _safe_get(c, "input", default=None) or _safe_get(c, "arguments", default=None)
                    if c_type in ("tool_use","tool_call") and name and isinstance(args, dict):
                        uses.append({"name": name, "arguments": args})
    tcalls = _safe_get(resp, "tool_calls", default=None) or _safe_get(resp, "choices", 0, "message", "tool_calls", default=None)
    if tcalls and isinstance(tcalls, (list, tuple)):
        for t in tcalls:
            name = _safe_get(t, "function", "name", default=None) or _safe_get(t, "name", default=None)
            argstr = _safe_get(t, "function", "arguments", default="{}")
            try:
                args = json.loads(argstr) if isinstance(argstr, str) else (argstr or {})
            except Exception:
                args = {}
            if name and isinstance(args, dict):
                uses.append({"name": name, "arguments": args})
    return uses

async def run_tools_if_any(ws, resp, caller_number: str | None):
    tool_uses = extract_tool_uses(resp)
    if not tool_uses:
        return False
    for call in tool_uses:
        nm, args = call.get("name"), call.get("arguments", {})
        if nm == "book_appointment":
            with section("tool.book_appointment"):
                try:
                    args = dict(args)
                    args.setdefault("duration_min", 30)
                    rec = save_booking(args)
                    jsonlog.info("booking.saved", record=rec, ics=str(ICS_DIR / f"{rec['id']}.ics"))
                    dt = rec["start"].replace('T',' ')[:16]
                    msg = f"Booked {rec['name']} on {dt}. I saved your appointment at {rec['address']}."
                    await send_text(ws, msg)
                except Exception as e:
                    jsonlog.error("booking.error", error=str(e))
                    await send_text(ws, "I had trouble saving that booking. Let’s try again.")
        elif nm == "mark_opt_out":
            with section("tool.mark_opt_out"):
                try:
                    args = dict(args)
                    args.setdefault("phone", caller_number or "")
                    rec = save_optout(args)
                    jsonlog.info("optout.saved", record=rec)
                    await send_text(ws, "Understood. I’ve marked you as do-not-contact.")
                except Exception as e:
                    jsonlog.error("optout.error", error=str(e))
                    await send_text(ws, "I couldn’t record that just now. I’ll try again if you wish.")
    return True


# ---------------- helpers ----------------
def _utc(dt: datetime) -> datetime:
    return dt.astimezone(ZoneInfo("UTC"))

def _ics_ts(dt: datetime) -> str:
    return _utc(dt).strftime("%Y%m%dT%H%M%SZ")

def _ics(uid: str, start_dt: datetime, end_dt: datetime, summary: str, desc: str) -> str:
    nowz = datetime.now(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    return "\\r\\n".join([
        "BEGIN:VCALENDAR","VERSION:2.0","PRODID:-//FRG//Chloe//EN","CALSCALE:GREGORIAN","METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}", f"DTSTAMP:{nowz}",
        f"DTSTART:{_ics_ts(start_dt)}", f"DTEND:{_ics_ts(end_dt)}",
        f"SUMMARY:{summary}", f"DESCRIPTION:{desc}",
        "END:VEVENT","END:VCALENDAR",""
    ])

def save_booking(args: dict) -> dict:
    sid = uuid.uuid4().hex[:12]
    start = datetime.fromisoformat(args["iso_start"]).astimezone(TZ)
    dur = int(args.get("duration_min", 30))
    end = start + timedelta(minutes=dur)
    rec = {
        "id": sid, "start": start.isoformat(), "end": end.isoformat(),
        "name": args.get("name",""), "address": args.get("address",""), "phone": args.get("phone",""),
        "note": args.get("note","Consultation")
    }
    (ICS_DIR / f"{sid}.ics").write_text(_ics(sid, start, end, f"{ORG_NAME} Consultation",
                                             f"Caller: {rec['phone']}; Name: {rec['name']}; Address: {rec['address']}"), encoding="utf-8")
    (BOOK_DIR / f"{start.date().isoformat()}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return rec

def save_optout(args: dict) -> dict:
    sid = uuid.uuid4().hex[:12]
    rec = {"id": sid, "type":"optout", "name": args.get("name",""), "address": args.get("address",""), "phone": args.get("phone","")}
    (BOOK_DIR / f"optout-{sid}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return rec

# ---------------- prompts & tools ----------------
SYSTEM_EN = (
    "Do not mention documents, uploads, tools, or vector stores. Keep answers to ≤2 short sentences, then ask a question. "
    "Prioritize booking using the 3-step flow: time → name on property → property address. "
    "Start with a greeting and offer English/Spanish; stay in the chosen language. Honor opt-out immediately. "
    "If interrupted, stop speaking and address the latest utterance."
)

SYSTEM_ES = (
    "No menciones documentos, cargas, herramientas ni almacenes vectoriales. Mantén respuestas en ≤2 frases y haz una pregunta. "
    "Prioriza agendar con el flujo de 3 pasos: hora → nombre en la propiedad → dirección. "
    "Empieza saludando y ofreciendo inglés/español; mantén el idioma elegido. Respeta el opt-out de inmediato. "
    "Si te interrumpen, detente y responde a lo último dicho."
)

SCHED_RE = re.compile(r"\\b(book|schedule|appointment|consult|cita|agendar|programar)\\b", re.I)
LANG_HINT_RE = re.compile(r"\\b(espanol|español|spanish|ingl[eé]s|english)\\b", re.I)

FUNCTION_TOOLS = [
    {
        "type": "function",
        "name": "book_appointment",
        "description": "Schedule a consult. Call only after the caller clearly agrees to schedule.",
        "parameters": {
            "type": "object",
            "properties": {
                "iso_start": {"type": "string", "description": "ISO-8601 start"},
                "duration_min": {"type": "integer", "default": 30},
                "name": {"type": "string"},
                "address": {"type": "string"},
                "phone": {"type": "string"},
                "note": {"type": "string"}
            },
            "required": ["iso_start", "name", "address"],
            "additionalProperties": False
        }
    },
    {
        "type": "function",
        "name": "mark_opt_out",
        "description": "Record a do-not-contact request.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "phone": {"type": "string"},
                "address": {"type": "string"}
            },
            "required": ["name"],
            "additionalProperties": False
        }
    }
]

def build_tools_for_user(user_text: str) -> list[dict]:
    ids = [i for i in [VECTOR_STORE_CALLSCRIPTS_ID, VECTOR_STORE_POLICIES_ID] if i]
    tools: list[dict] = []
    if ids:
        tools.append({"type": "file_search", "vector_store_ids": ids})
    tools.extend(FUNCTION_TOOLS)
    return tools

def validate_tools_or_die(tools: list[dict]) -> None:
    if not isinstance(tools, list):
        raise ValueError("tools must be a list")
    for idx, t in enumerate(tools):
        if t.get("type") == "file_search":
            ids = t.get("vector_store_ids") or []
            if not isinstance(ids, list) or not ids:
                raise ValueError("file_search tool missing vector_store_ids")
        elif t.get("type") == "function":
            if "name" not in t or "parameters" not in t:
                raise ValueError(f"function tool at {idx} must have top-level name and parameters")

# ---------------- http ----------------
@app.get("/")
async def index() -> PlainTextResponse:
    return PlainTextResponse("OK")

@app.get("/version")
async def version() -> JSONResponse:
    return JSONResponse({"app_version": APP_VERSION, "git_commit": GIT_COMMIT})

@app.post("/voice")
async def voice(_: Request) -> PlainTextResponse:
    twiml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <ConversationRelay url="{RELAY_WSS_URL}" transcriptionProvider="Deepgram" speechModel="nova-3-general" ttsProvider="Amazon">
      <Language code="en-US" voice="Joanna-Neural" />
      <Language code="es-US" voice="Lupe-Neural" />
    </ConversationRelay>
  </Connect>
</Response>'''
    return PlainTextResponse(twiml, media_type="text/xml")

# ---------------- ws ----------------
async def send_text(ws: WebSocket, text: str) -> None:
    await ws.send_json({"type":"text","token":text,"last":True})

async def cr_send(ws: WebSocket, token: str, last: bool=False) -> None:
    await ws.send_json({"type":"text","token":token,"last":last})

@app.websocket("/relay")
async def relay(ws: WebSocket) -> None:
    await ws.accept()
    print("ConversationRelay: connected", flush=True)
    caller_number: str | None = None
    history: list[dict] = []
    lang = "en-US"

    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")

            if mtype == "setup":
                with section("ws.setup"):
                    caller_number = (msg.get("from") or "").strip() or None
                    await cr_send(ws, f"Hi, this is Chloe with {ORG_NAME}. ")
                    await cr_send(ws, "Would you like to continue in English or Spanish?", last=True)
                continue

            if mtype in ("input_text","prompt"):
                with section("ws.rx"):
                    user_text = (msg.get("text") or msg.get("voicePrompt") or "").strip()
                    if not user_text:
                        continue
                    if LANG_HINT_RE.search(user_text):
                        if re.search(r"espanol|español|spanish", user_text, re.I):
                            lang = "es-US"
                            await ws.send_json({"type":"language","transcriptionLanguage":"es-US","ttsLanguage":"es-US"})
                            await send_text(ws, "Entendido. Puedo ayudarte en español.")
                            continue
                        if re.search(r"ingl[eé]s|english", user_text, re.I):
                            lang = "en-US"
                            await ws.send_json({"type":"language","transcriptionLanguage":"en-US","ttsLanguage":"en-US"})
                            await send_text(ws, "Got it. I’ll continue in English.")
                            continue

                    system = SYSTEM_ES if lang == "es-US" else SYSTEM_EN
                    history.append({"role":"user","content":user_text})

                    tools = build_tools_for_user(user_text)
                    validate_tools_or_die(tools)
                    jsonlog.info("tools.final", tools=tools)

                    try:
                        with section("openai.responses.create"):
                            resp = await client.responses.create(
                                model="gpt-4o-mini",
                                input=[{"role":"system","content":system}, *history[-12:]],
                                tools=tools,
                                temperature=0.3,
                                max_output_tokens=220,
                            )
                    except Exception as e:
                        print("OpenAI error:", repr(e), flush=True)
                        await send_text(ws, "Sorry, I had a problem—could you say that again?")
                        continue

                    # extract assistant text
                    text = ""
                    try:
                        text = (resp.output_text or "").strip()
                    except Exception:
                        pass
                    text = text or "Could you say that again?"
                    clean = redact_output(text)
                    history.append({"role":"assistant","content":clean})
                    await send_text(ws, clean)
                    # run tools if any
                    try:
                        ran = await run_tools_if_any(ws, resp, caller_number)
                        if ran:
                            jsonlog.info("tools.executed", ok=True)
                    except Exception as e:
                        jsonlog.error("tools.exec.fail", error=str(e))
                continue

            if mtype == "interrupt":
                await send_text(ws, "Understood—go ahead.")
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
