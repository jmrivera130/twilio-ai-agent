# app.py — FastAPI + Twilio ConversationRelay (Sept 2025) + OpenAI
# Five-phase flow with "snap-out" safety: 
#   1) language → 2) intro → 3) qna (info) ↔ 4) booking → 5) confirm/outro
# Robust phase switching: caller can ask info during booking (jump back to qna), or ask to talk to a person (route to booking).
# Name/address/phone collectors are interruption-aware; bounded retries avoid loops.
# /voice TwiML is minimal to satisfy Twilio's voice webhook timeout; <Language> entries and mid-call language switch supported.

import os, re, json, uuid
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.responses import PlainTextResponse, JSONResponse
from openai import OpenAI

# ---------- required env ----------
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
RELAY_WSS_URL  = os.environ["RELAY_WSS_URL"]   # wss://<your-app>.onrender.com/relay

# Business timezone (for parsing & storage)
BUSINESS_TZ = os.environ.get("TIMEZONE", "America/Los_Angeles")
TZ = ZoneInfo(BUSINESS_TZ)

# Storage dirs
BOOK_DIR   = Path(os.environ.get("BOOK_DIR", "/tmp/appointments"))
REPORT_DIR = Path(os.environ.get("REPORT_DIR", "/tmp/reports"))
ICS_DIR    = Path(os.environ.get("ICS_DIR", "/tmp/ics"))
BOOK_DIR.mkdir(parents=True, exist_ok=True)
REPORT_DIR.mkdir(parents=True, exist_ok=True)
ICS_DIR.mkdir(parents=True, exist_ok=True)

client = OpenAI(api_key=OPENAI_API_KEY)
app = FastAPI()

# ----------- Prompts -----------
SYSTEM_PROMPT_EN = (
    "You are Chloe from Foreclosure Relief Group. Be warm, concise, and clear. "
    "Prefer 1–3 short sentences. Avoid filler. Offer more detail only if asked. "
    "If the caller asks for help, offer to schedule a consultation. "
    "When a slot is active (date/time/name/address/phone/confirm), respond briefly and return to that task."
)
SYSTEM_PROMPT_ES = (
    "Eres Chloe del Foreclosure Relief Group. Habla en español claro y breve. "
    "Usa 1–3 frases cortas. Sin relleno. Da más detalles solo si te los piden. "
    "Si la persona pide ayuda, ofrece agendar una consulta. "
    "Cuando haya una tarea activa (fecha/hora/nombre/dirección/teléfono/confirmar), responde breve y vuelve a esa tarea."
)

# ----------- helpers -----------
async def send_text(ws: WebSocket, text: str, last: bool = True):
    # Stream a single token (we send whole sentences here) to ConversationRelay for TTS.
    await ws.send_json({"type": "text", "token": text, "last": last})

async def set_cr_language(ws: WebSocket, tts: str, stt: str):
    # Twilio ConversationRelay WS message to switch TTS/STT mid-call
    await ws.send_json({"type": "language", "ttsLanguage": tts, "transcriptionLanguage": stt})

def _day_path(d: date) -> Path:
    return BOOK_DIR / f"{d.isoformat()}.jsonl"

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

def _write_jsonl_for_day(day: date, rec: dict):
    p = _day_path(day)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def save_booking(start_dt: datetime, caller_number: str | None,
                 name: str | None, address: str | None,
                 note: str = "Consultation", duration_min: int = 30,
                 gcal_link: str | None = None):
    end_dt = start_dt + timedelta(minutes=duration_min)
    rec = {
        "type": "booking",
        "id": uuid.uuid4().hex[:12],
        "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "caller": caller_number or "",
        "name": (name or "").strip(),
        "address": (address or "").strip(),
        "note": note,
        "calendar_link": gcal_link or ""
    }
    ics_text = make_ics(rec["id"], start_dt, end_dt,
                        "Foreclosure Relief Consultation",
                        f"Caller: {rec['caller']}; Name: {rec['name']}; Address: {rec['address']}")
    (ICS_DIR / f"{rec['id']}.ics").write_text(ics_text, encoding="utf-8")
    _write_jsonl_for_day(start_dt.date(), rec)
    try:
        mirror = dict(rec)
        note_str = (mirror.get("note") or "").strip()
        mirror["note"] = "mirror=true" if not note_str else f"{note_str}; mirror=true"
        _write_jsonl_for_day(datetime.now(TZ).date(), mirror)
    except Exception:
        pass
    return rec

def save_optout(caller_number: str | None, name: str | None, address: str | None, note: str = "DNC request"):
    rec = {
        "type": "optout",
        "id": uuid.uuid4().hex[:12],
        "created_at": datetime.now(ZoneInfo("UTC")).isoformat(),
        "start": "", "end": "",
        "caller": caller_number or "",
        "name": (name or "").strip(),
        "address": (address or "").strip(),
        "note": note, "calendar_link": ""
    }
    _write_jsonl_for_day(datetime.now(TZ).date(), rec)
    return rec

def render_report_csv(day: date) -> str:
    header = [
        "id","record_type","created_at","caller","name","address",
        "appointment_start","appointment_end","note","calendar_link",
    ]
    p = _day_path(day); rows = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                j = json.loads(line)
                rows.append([
                    j.get("id",""), j.get("type",""), j.get("created_at",""),
                    j.get("caller","") or "", j.get("name","") or "", j.get("address","") or "",
                    j.get("start",""), j.get("end",""), j.get("note","") or "", j.get("calendar_link","") or "",
                ])
            except Exception:
                continue
    out = [",".join(header)]
    for r in rows:
        safe = ['"{}"'.format(str(x).replace('"','""')) for x in r]
        out.append(",".join(safe))
    return "\n".join(out)

# ---------- lightweight natural language date/time parsing ----------
WEEKDAYS = {"monday":0,"tuesday":1,"wednesday":2,"thursday":3,"friday":4,"saturday":5,"sunday":6,
            "lunes":0,"martes":1,"miercoles":2,"miércoles":2,"jueves":3,"viernes":4,"sabado":5,"sábado":5,"domingo":6}
MONTHS = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,"july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
          "enero":1,"febrero":2,"marzo":2,"abril":4,"mayo":5,"junio":6,"julio":7,"agosto":8,"septiembre":9,"setiembre":9,"octubre":10,"noviembre":11,"diciembre":12}

# Fix Spanish month mapping error above (marzo=3)
MONTHS["marzo"] = 3


def _next_weekday(now_dt: datetime, target: int) -> date:
    days_ahead = (target - now_dt.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return (now_dt + timedelta(days=days_ahead)).date()

def parse_date_phrase(text: str, now_dt: datetime) -> date | None:
    s = (text or "").lower()

    m = re.search(r"\bin\s+(\d+)\s+day[s]?\b|\ben\s+(\d+)\s+d[ií]a[s]?\b", s)
    if m:
        n = int([g for g in m.groups() if g][0])
        return (now_dt + timedelta(days=n)).date()

    if re.search(r"\btoday\b|\bhoy\b", s):
        return now_dt.date()
    if re.search(r"\btomorrow\b|\bma[ñn]ana\b|\bmañana\b", s):
        return (now_dt + timedelta(days=1)).date()

    for name, idx in WEEKDAYS.items():
        if re.search(rf"\b{name}\b", s):
            return _next_weekday(now_dt, idx)

    m = re.search(r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|enero|febrero|marzo|abril|mayo|junio|julio|agosto|sept(iembre|iembre)|octubre|noviembre|diciembre)\s+(\d{1,2})\b", s)
    if m:
        month_name = m.group(1)
        mon = None
        for full, num in MONTHS.items():
            if full.startswith(month_name[:3]):
                mon = num; break
        day = int(m.group(2)); year = now_dt.year
        try:
            d = date(year, mon, day)
            if d < now_dt.date():
                d = date(year + 1, mon, day)
            return d
        except Exception:
            pass

    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})\b", s)
    if m:
        mon, day = int(m.group(1)), int(m.group(2))
        year = now_dt.year
        try:
            d = date(year, mon, day)
            if d < now_dt.date():
                d = date(year + 1, mon, day)
            return d
        except Exception:
            pass

    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            pass

    return None

def parse_time_phrase(text: str) -> time | None:
    s = (text or "").lower().strip()
    s2 = s.replace(" ", "")
    if "noon" in s or "mediod" in s:
        return time(12, 0)
    if "midnight" in s or "medianoche" in s:
        return time(0, 0)
    m = re.search(r"\b(?:at\s*|a\s*las\s*)?(\d{1,2})(?::(\d{2}))?(a\.??m\.??|p\.??m\.??|am|pm)?\b", s)
    if not m:
        m = re.search(r"(?:^|[^0-9])(\d{1,2})(?::(\d{2}))?(a\.??m\.??|p\.??m\.??)?(?:$|[^0-9])", s2)
    if not m:
        return None
    hour = int(m.group(1)); minute = int(m.group(2) or 0); ap = (m.group(3) or "").replace(".", "")
    if ap == "pm" and hour != 12:
        hour += 12
    if ap == "am" and hour == 12:
        hour = 0
    if not ap and hour <= 7:
        hour += 12
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)

def extract_datetime(text: str):
    now_dt = datetime.now(TZ)
    d = parse_date_phrase(text, now_dt)
    t = parse_time_phrase(text)
    if d and t:
        return d, t, datetime.combine(d, t, tzinfo=TZ)
    return d, t, None

# ---------- helpers: intent & slot detection ----------
BOOKING_KEYWORDS_RE = re.compile(r"\b(book|appointment|schedule|set up|meeting|consult|cita|agendar|programar)\b", re.I)
YES_RE = re.compile(r"\b(yes|yeah|yep|correct|confirmed|that works|sounds good|ok|okay|si|sí|claro|correcto|de acuerdo)\b", re.I)
OPT_OUT_RE = re.compile(r"\b(opt\s*out|do\s*not\s*contact|do\s*not\s*call|don't\s*call|do not call|stop|unsubscribe|remove me|take me off|no me llames|no me contacten|quitar|baja)\b", re.I)
INFO_INTENT_RE = re.compile(r"\b(what\s+is\s+this\s+about|what\s+is\s+this|what\s+is\s+it\s+about|what's\s+this|que\s+es\s+esto|qué\s+es\s+esto|explica|explain|information|info|details)\b|\?", re.I)
HUMAN_INTENT_RE = re.compile(r"\b(human|agent|representative|person|alguien|agente|humano)\b", re.I)

NAME_RE = re.compile(r"\b(my name is|this is|me llamo|mi nombre es)\s+([A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ\.\-'\s]{1,60})\b", re.I)
ADDR_HINT_RE = re.compile(r"\b(address|property address|the address|la direccion|la dirección|la propiedad)\s*(?:is|es|:)?\s*(.+)", re.I)
STREET_RE = re.compile(r"\b\d{1,6}\s+[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ][A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ\s\.\-']{3,}\b")
PHONE_DIGITS = re.compile(r"\d")

MESSAGES = {
    "en": {
        "lang_choice": "Say 'English' or 'Español' to continue.",
        "lang_set": "Got it. I’ll continue in English.",
        "lang_set_es": "Entendido. Continuaré en español.",
        "intro": "I’m Chloe with Foreclosure Relief Group. I can answer questions or help schedule a consultation.",
        "ask_date": "What day works for you? (e.g., Tuesday or September 25)",
        "ask_time": "What time should I book? (e.g., 12 PM)",
        "ask_name": "Great — I have {when}. What’s your full name?",
        "ask_address": "Could you say the full property address, including the street and number?",
        "ask_phone": "What’s the best number to reach you?",
        "confirm_booking": "To confirm: {name} at {addr} on {when}. Is that correct?",
        "booked": "All set — you’re booked for {when}.",
        "opt_start": "Understood. I’ll mark you as do-not-contact. What’s your full name?",
        "opt_addr": "Thanks. What is the full property address from the notice?",
        "opt_phone": "What’s the best number to reach you?",
        "opt_confirm": "Confirm do-not-contact for {name} at {addr}, phone {phone}. Is that correct?",
        "opt_done": "You’re marked do-not-contact. Anything else?",
        "please_yes_no": "Please say yes or no to confirm.",
        "outro": "Thanks for calling. If you need anything else, just say so.",
        "last_name_prompt": "Got it — now say your last name.",
        "name_confirm_hint": "If that is your full name, say yes; otherwise add your last name.",
    },
    "es": {
        "lang_choice": "Di 'English' o 'Español' para continuar.",
        "lang_set": "Got it. I’ll continue in English.",
        "lang_set_es": "Entendido. Continuaré en español.",
        "intro": "Soy Chloe del Foreclosure Relief Group. Puedo responder preguntas o agendar una consulta.",
        "ask_date": "¿Qué día te funciona? (por ej., martes o 25 de septiembre)",
        "ask_time": "¿A qué hora agendamos? (por ej., 12 PM)",
        "ask_name": "Perfecto — tengo {when}. ¿Cuál es tu nombre completo?",
        "ask_address": "¿Puedes decir la dirección completa de la propiedad (calle y número)?",
        "ask_phone": "¿Cuál es el mejor número para contactarte?",
        "confirm_booking": "Para confirmar: {name} en {addr} el {when}. ¿Está bien?",
        "booked": "Listo — tu cita es el {when}.",
        "opt_start": "Entendido. Te pondré en no-contactar. ¿Cuál es tu nombre completo?",
        "opt_addr": "Gracias. ¿Cuál es la dirección completa indicada en el aviso?",
        "opt_phone": "¿Cuál es el mejor número para contactarte?",
        "opt_confirm": "Confirma no-contactar para {name} en {addr}, teléfono {phone}. ¿Correcto?",
        "opt_done": "Quedaste en no-contactar. ¿Algo más?",
        "please_yes_no": "Por favor di sí o no para confirmar.",
        "outro": "Gracias por llamar. Si necesitas algo más, dímelo.",
        "last_name_prompt": "Entendido — ahora di tu apellido.",
        "name_confirm_hint": "Si ese es tu nombre completo, di sí; si no, agrega tu apellido.",
    }
}

class NameCollector:
    """Interruption-aware name collector: accepts 'First Last' in one turn or binds First then tail.
       Tracks simple repeats and allows 'yes' to confirm single-token names."""
    def __init__(self):
        self.first = None
        self.repeats = 0

    def need_tail(self):
        return bool(self.first and not isinstance(self.first, list))

    def observe(self, utter: str):
        s = (utter or "").strip()
        # full name fast path
        m_full = re.search(r"\b([A-Za-zÁÉÍÓÚÜÑáéíóúüñ][\w'\-]{1,})\s+([A-Za-zÁÉÍÓÚÜÑáéíóúüñ][\w'\-]{1,})\b", s)
        if m_full:
            full = (m_full.group(1) + " " + m_full.group(2)).strip()
            return True, full
        # capture first token
        m_first = re.search(r"\b([A-Za-zÁÉÍÓÚÜÑáéíóúüñ][\w'\-]{1,})\b", s)
        if m_first and not self.first:
            self.first = m_first.group(1)
            return False, None
        # tail
        if self.first:
            m_last = re.search(r"\b([A-Za-zÁÉÍÓÚÜÑáéíóúüñ][\w'\-]{1,})\b", s)
            if m_last and m_last.group(1).lower() != self.first.lower():
                return True, f"{self.first} {m_last.group(1)}"
            else:
                self.repeats += 1
        return False, None

def maybe_extract_name(text: str) -> str | None:
    m = NAME_RE.search(text or "")
    if m:
        return m.group(2).strip(" .,'-")
    return None

def maybe_extract_address(text: str) -> str | None:
    m = ADDR_HINT_RE.search(text or "")
    if m:
        addr = m.group(1 if "address" in m.group(0).lower() else 2).strip()
        if STREET_RE.search(addr):
            return addr
    m2 = STREET_RE.search(text or "")
    if m2:
        return m2.group(0).strip()
    return None

def is_full_street_address(text: str) -> bool:
    return bool(text and STREET_RE.search((text or "").strip()))

def maybe_extract_phone(text: str) -> str | None:
    digits = re.sub(r"\D+", "", text or "")
    if 10 <= len(digits) <= 15:
        if len(digits) == 10:
            return "+1" + digits
        if digits.startswith("1") and len(digits) == 11:
            return "+" + digits
        if digits.startswith("+"):
            return digits
        return "+" + digits
    return None

def _tz_label(dt: datetime) -> str:
    try:
        if "Los_Angeles" in BUSINESS_TZ:
            return "Pacific"
        return dt.tzname() or BUSINESS_TZ
    except Exception:
        return BUSINESS_TZ

def when_phrase(dt: datetime, lang: str = "en") -> str:
    base = dt.strftime('%A, %B %d, %I:%M %p') if lang == 'en' else dt.strftime('%A %d de %B, %I:%M %p')
    return f"{base} {_tz_label(dt)}"

# ---------- HTTP: Twilio hits /voice (tiny TwiML) ----------
@app.post("/voice")
async def voice(_: Request):
    # Minimal TwiML, immediate return (Twilio voice webhooks have a strict time cap)
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <ConversationRelay
      url="{RELAY_WSS_URL}"
      ttsProvider="Amazon"
      voice="Joanna-Neural"
      language="en-US"
      reportInputDuringAgentSpeech="speech"
      interruptible="any"
      hints="Foreclosure, Foreclosure Relief Group, consultation, address, appointment, cita, Español, English, John, Smith, Snow, Lopez">
      <Language code="en-US"
                ttsProvider="Amazon"
                voice="Joanna-Neural"
                transcriptionProvider="Deepgram"
                speechModel="nova-3-general" />
      <Language code="es-ES"
                ttsProvider="Amazon"
                voice="Conchita-Neural"
                transcriptionProvider="Deepgram"
                speechModel="nova-3-general" />
    </ConversationRelay>
  </Connect>
</Response>"""
    return PlainTextResponse(twiml, media_type="text/xml")

@app.get("/")
async def index():
    return PlainTextResponse("OK")

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})

@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True})

@app.head("/")
async def head_root():
    return Response(status_code=200)

@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)

@app.get("/ics/{bid}.ics")
async def get_ics(bid: str):
    p = ICS_DIR / f"{bid}.ics"
    if p.exists():
        return PlainTextResponse(p.read_text(encoding="utf-8"), media_type="text/calendar; charset=utf-8")
    return Response(status_code=404)

@app.get("/reports/today")
async def report_today():
    d = datetime.now(TZ).date()
    csv_text = render_report_csv(d)
    fname = f"appointments-{d.isoformat()}.csv"
    return PlainTextResponse(csv_text, media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

@app.get("/reports/{day}")
async def report_day(day: str):
    try:
        d = datetime.fromisoformat(day).date()
    except Exception:
        return JSONResponse({"error":"bad date"}, status_code=400)
    csv_text = render_report_csv(d)
    fname = f"appointments-{d.isoformat()}.csv"
    return PlainTextResponse(csv_text, media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

# ---------- WebSocket: Twilio connects here ----------
@app.websocket("/relay")
async def relay(ws: WebSocket):
    await ws.accept()
    print("ConversationRelay: connected", flush=True)

    history: list[dict] = []

    caller_number: str | None = None
    state = {
        "phase": "language",  # language -> intro -> qna -> booking -> outro
        "lang": None,         # "en" or "es"
        "lang_prompted": False,
        "mode": None,         # None | "booking" | "optout"
        "need": None,         # None | "date" | "time" | "name" | "address" | "phone" | "confirm"
        "capture_next": None, # None | "name" | "address" | "phone"
        "name_collector": None,
        "intro_done": False,

        "hold_date": None,
        "hold_time": None,
        "hold_dt": None,
        "hold_name": None,
        "hold_address": None,
        "hold_phone": None,

        "retries": 0,         # per-slot small counter
    }

    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")

            if mtype == "setup":
                caller_number = (msg.get("from") or "").strip() or None
                continue

            if mtype == "prompt":
                user_text = (msg.get("voicePrompt") or "").strip()
                if not user_text:
                    continue
                print(f"RX: {user_text} | phase={state['phase']} need={state['need']} cap={state['capture_next']}", flush=True)

                # --- Phase 1: Language selection ---
                if not state["lang"]:
                    s = user_text.lower()
                    if re.search(r"\b(esp[aá]nol|spanish|2)\b", s):
                        state["lang"] = "es"
                        await set_cr_language(ws, "es-ES", "es-ES")
                        await send_text(ws, MESSAGES["es"]["lang_set_es"])
                        await send_text(ws, MESSAGES["es"]["intro"])
                        state["phase"] = "qna"
                        continue
                    if re.search(r"\b(english|ingl[eé]s|1)\b", s):
                        state["lang"] = "en"
                        await set_cr_language(ws, "en-US", "en-US")
                        await send_text(ws, MESSAGES["en"]["lang_set"])
                        await send_text(ws, MESSAGES["en"]["intro"])
                        state["phase"] = "qna"
                        continue
                    if not state["lang_prompted"]:
                        state["lang_prompted"] = True
                        await send_text(ws, MESSAGES["en"]["lang_choice"])
                        continue
                    # default to English
                    state["lang"] = "en"
                    await set_cr_language(ws, "en-US", "en-US")
                    await send_text(ws, MESSAGES["en"]["intro"])
                    state["phase"] = "qna"
                    continue

                lang = state["lang"]
                MSG = MESSAGES[lang]
                sys_prompt = SYSTEM_PROMPT_EN if lang == "en" else SYSTEM_PROMPT_ES

                # ---- Global intents (can snap phases) ----
                if OPT_OUT_RE.search(user_text):
                    state.update({"mode":"optout"})
                    if not state["hold_name"]:
                        state["need"] = "name"; state["capture_next"]="name"; state["name_collector"]=NameCollector()
                        await send_text(ws, MSG["opt_start"]); continue
                    if not state["hold_address"]:
                        state["need"] = "address"; state["capture_next"]="address"
                        await send_text(ws, MSG["opt_addr"]); continue
                    if not caller_number and not state["hold_phone"]:
                        state["need"] = "phone"; state["capture_next"]="phone"
                        await send_text(ws, MSG["opt_phone"]); continue
                    ph = state["hold_phone"] or caller_number
                    state["need"] = "confirm"; await send_text(ws, MSG["opt_confirm"].format(name=state["hold_name"], addr=state["hold_address"], phone=ph)); continue

                # HUMAN intent routes to booking (Phase 4)
                if HUMAN_INTENT_RE.search(user_text):
                    state["mode"] = "booking"

                # Extract date/time early for intent
                d, t, dt_comb = extract_datetime(user_text)
                booking_keyword = bool(BOOKING_KEYWORDS_RE.search(user_text))
                info_intent = bool(INFO_INTENT_RE.search(user_text))

                # Phase snap logic
                if state["mode"] == "booking" and info_intent:
                    # Caller asked info while in booking → snap back to Q&A
                    state["mode"] = None; state["need"] = None; state["capture_next"] = None
                    await send_text(ws, "Sure — here’s a quick overview. Feel free to ask follow-ups.")
                    state["phase"] = "qna"

                if state["mode"] is None and (booking_keyword or d or t):
                    state["mode"] = "booking"

                # ---------- Phase 4: Booking (slot-fill) ----------
                if state["mode"] == "booking":
                    if d: state["hold_date"] = d
                    if t: state["hold_time"] = t
                    if state.get("hold_date") and state.get("hold_time"):
                        state["hold_dt"] = datetime.combine(state["hold_date"], state["hold_time"], tzinfo=TZ)

                    if not state.get("hold_date"):
                        state["need"] = "date"; state["retries"]=0
                        await send_text(ws, MSG["ask_date"]); continue
                    if not state.get("hold_time"):
                        state["need"] = "time"; state["retries"]=0
                        await send_text(ws, MSG["ask_time"]); continue
                    if not state.get("hold_name"):
                        if state.get("capture_next") != "name":
                            state["capture_next"] = "name"; state["name_collector"] = NameCollector()
                        state["need"] = "name"
                        when_say = when_phrase(state["hold_dt"], lang) if state.get("hold_dt") else ""
                        await send_text(ws, MSG["ask_name"].format(when=when_say)); continue
                    if not state.get("hold_address"):
                        state["need"] = "address"; state["capture_next"]="address"; state["retries"]=0
                        await send_text(ws, MSG["ask_address"]); continue
                    if not caller_number and not state.get("hold_phone"):
                        state["need"] = "phone"; state["capture_next"]="phone"; state["retries"]=0
                        await send_text(ws, MSG["ask_phone"]); continue

                    state["need"] = "confirm"
                    when_say = when_phrase(state["hold_dt"], lang)
                    await send_text(ws, MSG["confirm_booking"].format(name=state["hold_name"], addr=state["hold_address"], when=when_say))
                    continue

                # ---------- Active slot capture (Phase 4 details) ----------
                if state.get("capture_next") in {"name", "address", "phone"}:
                    cap = state["capture_next"]

                    if cap == "name":
                        if not state.get("name_collector"):
                            state["name_collector"] = NameCollector()
                        if YES_RE.search(user_text) and state["name_collector"].need_tail():
                            first_only = state["name_collector"].first or ""
                            if first_only:
                                state["hold_name"] = first_only
                                print(f"CAPTURED name={state['hold_name']}", flush=True)
                                state["capture_next"] = None
                                state["need"] = None
                                await send_text(ws, MSG["ask_address"])
                                state["capture_next"] = "address"
                                continue
                        done, full = state["name_collector"].observe(user_text)
                        if done and full:
                            state["hold_name"] = full
                            print(f"CAPTURED name={state['hold_name']}", flush=True)
                            state["capture_next"] = None
                            state["need"] = None
                            await send_text(ws, MSG["ask_address"])
                            state["capture_next"] = "address"
                            continue
                        if state["name_collector"].need_tail():
                            await send_text(ws, MSG["last_name_prompt"]) ; state["capture_next"] = "name" ; continue
                        if state["name_collector"].repeats >= 2:
                            first_only = state["name_collector"].first or ""
                            await send_text(ws, f"I heard your name as {first_only}. {MSG['name_confirm_hint']}")
                            state["capture_next"] = "name" ; continue
                        state["retries"] += 1
                        if state["retries"] > 2:
                            state["mode"] = None; state["need"] = None; state["capture_next"] = None
                            await send_text(ws, "No worries — we can come back to that. What questions do you have?")
                            state["phase"] = "qna" ; continue
                        when_say = when_phrase(state.get("hold_dt") or datetime.now(TZ), lang)
                        await send_text(ws, MSG["ask_name"].format(when=when_say))
                        state["capture_next"] = "name" ; continue

                    if cap == "address":
                        addr = (maybe_extract_address(user_text) or user_text).strip()
                        if is_full_street_address(addr):
                            state["hold_address"] = addr
                            state["capture_next"] = None
                            state["need"] = None
                            if not caller_number and not state.get("hold_phone"):
                                state["capture_next"] = "phone" ; await send_text(ws, MSG["ask_phone"]) ; continue
                            state["mode"] = "booking" ; state["need"] = "confirm"
                            when_say = when_phrase(state["hold_dt"], lang) if state.get("hold_dt") else ""
                            await send_text(ws, MSG["confirm_booking"].format(name=state["hold_name"], addr=state["hold_address"], when=when_say))
                            continue
                        state["retries"] += 1
                        if state["retries"] > 2:
                            state["mode"] = None; state["need"] = None; state["capture_next"] = None
                            await send_text(ws, "We can return to the address later. What else can I help with?")
                            state["phase"] = "qna" ; continue
                        await send_text(ws, MSG["ask_address"]) ; continue

                    if cap == "phone":
                        ph = maybe_extract_phone(user_text)
                        if ph:
                            state["hold_phone"] = ph
                            state["capture_next"] = None
                            state["need"] = "confirm"
                            when_say = when_phrase(state["hold_dt"], lang) if state.get("hold_dt") else ""
                            await send_text(ws, MSG["confirm_booking"].format(name=state["hold_name"], addr=state["hold_address"], when=when_say))
                            continue
                        state["retries"] += 1
                        if state["retries"] > 2:
                            state["mode"] = None; state["need"] = None; state["capture_next"] = None
                            await send_text(ws, "Let’s circle back on the number later. What else can I help with?")
                            state["phase"] = "qna" ; continue
                        await send_text(ws, MSG["ask_phone"]) ; continue

                # ---------- Phase 5: Confirm ----------
                if state["mode"] == "booking" and state.get("need") == "confirm":
                    if YES_RE.search(user_text):
                        start_dt = state["hold_dt"] or (datetime.now(TZ) + timedelta(days=1, hours=1))
                        phone_used = state["hold_phone"] or caller_number or ""
                        save_booking(start_dt, phone_used, state["hold_name"], state["hold_address"])
                        when_say = when_phrase(start_dt, lang)
                        await send_text(ws, MSG["booked"].format(when=when_say))
                        await send_text(ws, MSG["outro"])
                        state.update({"phase":"outro","mode":None,"need":None,"capture_next":None,
                                      "hold_date":None,"hold_time":None,"hold_dt":None,"hold_name":None,"hold_address":None,"hold_phone":None})
                        continue
                    await send_text(ws, MSG["please_yes_no"]) ; continue

                # ---------- Phase 3: Q&A (nuance) ----------
                try:
                    resp = client.responses.create(
                        model="gpt-4o-mini",
                        input=[
                            {"role":"system","content": sys_prompt},
                            *history[-6:],
                            {"role":"user","content": user_text},
                        ],
                        max_output_tokens=180,
                        temperature=0.3,
                    )
                    ai_text = (resp.output_text or "").strip() or ("Sorry, could you repeat that?" if lang=='en' else "¿Podrías repetir, por favor?")
                except Exception as e:
                    print("OpenAI error:", repr(e), flush=True)
                    ai_text = "I’m having trouble right now. Please say that again." if lang=='en' else "Tengo un problema ahora. Por favor, repite."

                print("TX:", ai_text, flush=True)
                history.append({"role": "user", "content": user_text})
                history.append({"role": "assistant", "content": ai_text})
                await send_text(ws, ai_text)
                continue

            if mtype == "interrupt":
                print("Interrupted:", msg.get("utteranceUntilInterrupt", ""), flush=True)
                if state.get("need") in {"name","address","phone"}:
                    state["capture_next"] = state["need"]
                continue

            if mtype == "error":
                print("ConversationRelay error:", msg.get("description"), flush=True)
                continue

    except WebSocketDisconnect:
        print("ConversationRelay: disconnected", flush=True)
