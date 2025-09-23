# app.py — FastAPI + Twilio Conversation Relay + OpenAI (EN/ES, single number)
# Minimal, surgical edits only: keep existing behavior; add lightweight language choice.
# No TwiML attribute changes; voice stays as in your working file.
# Adds: language state (en/es), Spanish prompts/flows mirroring booking + opt-out.

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

SYSTEM_PROMPT_EN = (
    "You are Chloe from Foreclosure Relief Group. Be warm, concise, and clear. "
    "Prefer 1–3 short sentences. Avoid filler. Offer more detail only if asked. "
    "If the caller asks for help, offer to schedule a consultation."
)
SYSTEM_PROMPT_ES = (
    "Eres Chloe del Foreclosure Relief Group. Habla en español claro y breve. "
    "Usa 1–3 frases cortas. Sin relleno. Ofrece detalles solo si te los piden. "
    "Si la persona pide ayuda, ofrece agendar una consulta."
)

# ---------- utils ----------
async def send_text(ws: WebSocket, text: str):
    s = (text or "").strip()
    if not s:
        return
    chunks = []
    buf = ""
    for token in s.split():
        if len(buf) + 1 + len(token) > 40:
            if buf:
                chunks.append(buf)
            buf = token
        else:
            buf = token if not buf else buf + " " + token
    if buf:
        chunks.append(buf)
    if not chunks:
        chunks = [s]
    for c in chunks[:-1]:
        await ws.send_json({"type": "text", "token": c, "last": False})
    await ws.send_json({"type": "text", "token": chunks[-1], "last": True})

async def set_cr_language(ws: WebSocket, tts: str, stt: str):
    # Twilio ConversationRelay WS message to switch TTS/STT mid-call
    await ws.send_json({
        "type": "language",
        "ttsLanguage": tts,
        "transcriptionLanguage": stt
    })

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
        "caller": caller_number,
        "name": (name or "").strip(),
        "address": (address or "").strip(),
        "note": note,
        "calendar_link": gcal_link or ""
    }
    ics_text = make_ics(rec["id"], start_dt, end_dt,
                        "Foreclosure Relief Consultation",
                        f"Caller: {caller_number or 'unknown'}; Name: {rec['name']}; Address: {rec['address']}")
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
        "caller": caller_number,
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
          "enero":1,"febrero":2,"marzo":3,"abril":4,"mayo":5,"junio":6,"julio":7,"agosto":8,"septiembre":9,"setiembre":9,"octubre":10,"noviembre":11,"diciembre":12}

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
    m = re.search(r"\b(?:at\s*|a\s*las\s*)?(\d{1,2})(?::(\d{2}))?(a\.?m\.?|p\.?m\.?|am|pm)?\b", s)
    if not m:
        m = re.search(r"(?:^|[^0-9])(\d{1,2})(?::(\d{2}))?(a\.?m\.?|p\.?m\.?)?(?:$|[^0-9])", s2)
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

# ---------- helpers: booking & opt-out detection ----------
BOOKING_KEYWORDS_RE = re.compile(r"\b(book|appointment|schedule|set up|meeting|consult|cita|agendar|programar)\b", re.I)
YES_RE = re.compile(r"\b(yes|yeah|yep|correct|confirmed|that works|sounds good|ok|okay|si|sí|claro|correcto|de acuerdo)\b", re.I)
SCHEDULING_HINT_RE = re.compile(r"\b(schedule|book|appointment|set up|consult|cita|agendar|programar)\b", re.I)
OPT_OUT_RE = re.compile(r"\b(opt\s*out|do\s*not\s*contact|do\s*not\s*call|don't\s*call|do not call|stop|unsubscribe|remove me|take me off|no me llames|no me contacten|quitar|baja)\b", re.I)

NAME_RE = re.compile(r"\b(my name is|this is|me llamo|mi nombre es)\s+([A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ\.\-\'\s]{1,60})\b", re.I)
ADDR_HINT_RE = re.compile(r"\b(address|property address|the address|la direccion|la dirección|la propiedad)\s*(?:is|es|:)?\s*(.+)", re.I)
STREET_RE = re.compile(r"\b\d{1,6}\s+[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ][A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ\s\.\-']{3,}\b")
PHONE_DIGITS = re.compile(r"\d")

MESSAGES = {
    "en": {
        "lang_choice": "Say 'English' or 'Español' to continue.",
        "lang_set": "Got it. I’ll continue in English.",
        "lang_set_es": "Entendido. Continuaré en español.",
        "ask_date": "What day works for you? (e.g., Tuesday or September 15)",
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
    },
    "es": {
        "lang_choice": "Di 'English' o 'Español' para continuar.",
        "lang_set": "Got it. I’ll continue in English.",
        "lang_set_es": "Entendido. Continuaré en español.",
        "ask_date": "¿Qué día te funciona? (por ej., martes o 15 de septiembre)",
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
    }
}

def maybe_extract_name(text: str) -> str | None:
    m = NAME_RE.search(text)
    if m:
        return m.group(2).strip(" .,'-")
    return None

def maybe_extract_address(text: str) -> str | None:
    m = ADDR_HINT_RE.search(text)
    if m:
        addr = m.group(1 if "address" in m.group(0).lower() else 2).strip()
        if STREET_RE.search(addr):
            return addr
    m2 = STREET_RE.search(text or "")
    if m2:
        return m2.group(0).strip()
    return None

def is_full_street_address(text: str) -> bool:
    return bool(text and STREET_RE.search(text.strip()))



class NameCollector:
    def __init__(self):
        self.first = None
        self.last = None
        self.repeats = 0
        self.last_input = ""

    @staticmethod
    def tokens(text: str):
        s = (text or "").strip()
        s = re.sub(r"[^\wÁÉÍÓÚÜÑáéíóúüñ'\-\s]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        toks = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ'\-]{0,60}", s)
        return toks

    def observe(self, text: str):
        toks = self.tokens(text)
        norm = " ".join(toks).lower()
        if norm and norm == (self.last_input or "").lower():
            self.repeats += 1
        else:
            self.repeats = 0
        self.last_input = " ".join(toks)
        if len(toks) >= 2:
            self.first, self.last = toks[0], toks[1]
            return True, f"{self.first} {self.last}"
        if len(toks) == 1:
            if not self.first:
                self.first = toks[0]
                return False, None
            else:
                self.last = toks[0]
                return True, f"{self.first} {self.last}"
        return False, None

    def need_tail(self) -> bool:
        return bool(self.first and not self.last)

    def value(self):
        if self.first and self.last:
            return f"{self.first} {self.last}"
        return None
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

# ---------- HTTP: Twilio hits /voice (unchanged TwiML except URL/voice come from your env/code) ----------
@app.post("/voice")
async def voice(_: Request):
    # Minimal, fast TwiML for ConversationRelay (no blocking, returns immediately)
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <ConversationRelay
      url="{RELAY_WSS_URL}"
      ttsProvider="Amazon"
      voice="Joanna-Neural"
      reportInputDuringAgentSpeech="speech"
      interruptible="any"
      hints="Foreclosure, Foreclosure Relief Group, consultation, address, appointment, cita, español, English, John, Smith, Snow, Lopez"
    />
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
        "lang": None,         # "en" or "es"
        "lang_prompted": False,
        "mode": None,         # None | "booking" | "optout"
        "need": None,         # None | "date" | "time" | "name" | "address" | "phone" | "confirm"
        "hold_date": None,
        "hold_time": None,
        "hold_dt": None,
        "hold_name": None,
        "hold_address": None,
        "hold_phone": None,
        "offered_booking": False,
        "capture_next": None,
        "name_collector": None,
        "name_buffer": [],
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
                print("RX:", user_text, flush=True)
                
                # Active-need gate: satisfy pending field before any other logic
                # ---------- active slot capture ----------
                if state.get("capture_next") in {"name", "name_tail", "address", "phone"}:
                    cap = state["capture_next"]

                    # NAME (interruption-aware, two-turn)
                    if cap == "name":
                        if not state.get("name_collector"):
                            state["name_collector"] = NameCollector()
                        done, full = state["name_collector"].observe(user_text)

                        if done and full:
                            # got "First Last"
                            state["hold_name"] = full
                            state["capture_next"] = None
                            state["need"] = "address"
                            await send_text(ws, MESSAGES[state["lang"]]["ask_address"])
                            continue

                        if state["name_collector"].need_tail():
                            # we have First, bind for Last
                            await send_text(ws, "Got it — now say your last name.")
                            state["capture_next"] = "name"   # keep binder active
                            continue

                        if state["name_collector"].repeats >= 2:
                            # single token repeated — offer confirm
                            first_only = state["name_collector"].first or ""
                            await send_text(ws, f"I heard your name as {first_only}. If that is your full name, say yes; otherwise add your last name.")
                            state["capture_next"] = "name"
                            continue

                        # fallback re-ask (rare)
                        when_say = when_phrase(state.get("hold_dt") or datetime.now(TZ), state["lang"])
                        await send_text(ws, MESSAGES[state["lang"]]["ask_name"].format(when=when_say))
                        state["capture_next"] = "name"
                        continue

                    # ADDRESS
                    if cap == "address":
                        addr = (maybe_extract_address(user_text) or user_text).strip()
                        if is_full_street_address(addr):
                            state["hold_address"] = addr
                            state["capture_next"] = None
                            state["need"] = "phone" if (not caller_number and not state["hold_phone"]) else "confirm"
                            if state["need"] == "phone":
                                await send_text(ws, MESSAGES[state["lang"]]["ask_phone"])
                                continue
                            when_say = when_phrase(state["hold_dt"], state["lang"])
                            await send_text(ws, MESSAGES[state["lang"]]["confirm_booking"].format(
                                name=state["hold_name"], addr=state["hold_address"], when=when_say))
                            continue

                        await send_text(ws, MESSAGES[state["lang"]]["ask_address"])
                        state["capture_next"] = "address"
                        continue

                    # PHONE
                    if cap == "phone":
                        ph = maybe_extract_phone(user_text)
                        if ph:
                            state["hold_phone"] = ph
                            state["capture_next"] = None
                            state["need"] = "confirm"
                            when_say = when_phrase(state["hold_dt"], state["lang"])
                            await send_text(ws, MESSAGES[state["lang"]]["confirm_booking"].format(
                                name=state["hold_name"], addr=state["hold_address"], when=when_say))
                            continue

                        await send_text(ws, MESSAGES[state["lang"]]["ask_phone"])
                        state["capture_next"] = "phone"
                        continue

                # Fallback: legacy need-gate (still helpful if capture_next not set)
                # ---------- legacy slot fallback (address/phone only) ----------
                if state.get("need") in {"address", "phone"}:
                    need_now = state["need"]

                    if need_now == "address":
                        addr = (maybe_extract_address(user_text) or user_text).strip()
                        if is_full_street_address(addr):
                            state["hold_address"] = addr
                            state["need"] = "phone" if (not caller_number and not state["hold_phone"]) else "confirm"
                            if state["need"] == "phone":
                                await send_text(ws, MESSAGES[state["lang"]]["ask_phone"])
                                continue
                            when_say = when_phrase(state["hold_dt"], state["lang"])
                            await send_text(ws, MESSAGES[state["lang"]]["confirm_booking"].format(
                                name=state["hold_name"], addr=state["hold_address"], when=when_say))
                            continue
                        await send_text(ws, MESSAGES[state["lang"]]["ask_address"])
                        continue

                    if need_now == "phone":
                        ph = maybe_extract_phone(user_text)
                        if ph:
                            state["hold_phone"] = ph
                            state["need"] = "confirm"
                            when_say = when_phrase(state["hold_dt"], state["lang"])
                            await send_text(ws, MESSAGES[state["lang"]]["confirm_booking"].format(
                                name=state["hold_name"], addr=state["hold_address"], when=when_say))
                            continue
                        await send_text(ws, MESSAGES[state["lang"]]["ask_phone"])
                        continue

                # --- Language selection (single-number menu) ---
                if not state["lang"]:
                    s = user_text.lower()
                    if re.search(r"\b(esp[aá]nol|spanish|2)\b", s):
                        state["lang"] = "es"
                        await set_cr_language(ws, "es-ES", "es-ES")
                        await send_text(ws, MESSAGES["es"]["lang_set_es"])
                        await send_text(ws, "Soy Chloe del Foreclosure Relief Group. Puedo responder preguntas o agendar una consulta. ¿En qué te ayudo?")
                        state["offered_booking"] = True
                        continue

                    if re.search(r"\b(english|ingl[eé]s|1)\b", s):
                        state["lang"] = "en"
                        await set_cr_language(ws, "en-US", "en-US")
                        await send_text(ws, MESSAGES["en"]["lang_set"])
                        await send_text(ws, "I’m Chloe with Foreclosure Relief Group. I can answer questions or help schedule a consultation. How can I help?")
                        state["offered_booking"] = True
                        continue

                    if not state["lang_prompted"]:
                        state["lang_prompted"] = True
                        await send_text(ws, MESSAGES["en"]["lang_choice"]) 
                        continue
                    # If still unknown after prompt, default to English
                    state["lang"] = "en"
                    await set_cr_language(ws, "en-US", "en-US")


                lang = state["lang"]
                MSG = MESSAGES[lang]
                sys_prompt = SYSTEM_PROMPT_EN if lang == "en" else SYSTEM_PROMPT_ES

                # --- Quick date clarification ---
                if re.search(r"\b(what\s+(date|day)\s+is\s+that|which\s+day\s+is\s+that)\b|\b(qué\s+d[ií]a|qu[eé]\s+fecha)\b", user_text, re.I):
                    if state.get("hold_dt"):
                        await send_text(ws, when_phrase(state["hold_dt"], lang)); continue

                # --- Opt-out anytime ---
                if OPT_OUT_RE.search(user_text):
                    state.update({"mode":"optout"})
                    if not state["hold_name"]:
                        state["need"] = "name"
                        state["capture_next"] = "name"
                        state["name_collector"] = NameCollector()
                        when_say = when_phrase(state["hold_dt"], lang)
                        await send_text(ws, MSG["ask_name"].format(when=when_say))
                        continue
                    if not state["hold_address"]:
                        state["need"] = "address"; state["capture_next"] = "address"; await send_text(ws, MSG["ask_address"]); continue
                    if not caller_number and not state["hold_phone"]:
                        state["need"] = "phone"; state["capture_next"] = "phone"; await send_text(ws, MSG["ask_phone"]); continue
                    state["need"] = "confirm"; when_say = when_phrase(state["hold_dt"], lang)
                    await send_text(ws, MSG["confirm_booking"].format(name=state["hold_name"], addr=state["hold_address"], when=when_say)); continue

                if state["mode"] == "optout":
                    if state["need"] == "name":
                        nm = user_text.strip()
                        if len(nm) < 2: await send_text(ws, MSG["ask_name"].format(when="")); continue
                        state["hold_name"] = nm; state["need"] = "address"; await send_text(ws, MSG["opt_addr"]); continue
                    if state["need"] == "address":
                        addr = user_text.strip()
                        if not is_full_street_address(addr): await send_text(ws, MSG["opt_addr"]); continue
                        state["hold_address"] = addr
                        if not caller_number and not state["hold_phone"]:
                            state["need"] = "phone"; await send_text(ws, MSG["opt_phone"]); continue
                        ph = state["hold_phone"] or caller_number
                        state["need"] = "confirm"; await send_text(ws, MSG["opt_confirm"].format(name=state["hold_name"], addr=state["hold_address"], phone=ph)); continue
                    if state["need"] == "phone":
                        ph = maybe_extract_phone(user_text)
                        if not ph: await send_text(ws, MSG["opt_phone"]); continue
                        state["hold_phone"] = ph
                        state["need"] = "confirm"; await send_text(ws, MSG["opt_confirm"].format(name=state["hold_name"], addr=state["hold_address"], phone=ph)); continue
                    if state["need"] == "confirm":
                        if YES_RE.search(user_text):
                            ph = state["hold_phone"] or caller_number
                            save_optout(ph, state["hold_name"], state["hold_address"])
                            await send_text(ws, MSG["opt_done"])
                            state.update({"mode":None, "need":None, "hold_date":None, "hold_time":None, "hold_dt":None, "hold_name":None, "hold_address":None, "hold_phone":None})
                            continue
                        await send_text(ws, MSG["please_yes_no"]); continue

                if state["mode"] == "booking" and state["need"] == "confirm":
                    if YES_RE.search(user_text):
                        start_dt = state["hold_dt"]
                        phone_used = state["hold_phone"] or caller_number
                        rec = save_booking(start_dt, phone_used, state["hold_name"], state["hold_address"])
                        when_say = when_phrase(start_dt, lang)
                        await send_text(ws, MSG["booked"].format(when=when_say))
                        state.update({"mode":None, "need":None, "hold_date":None, "hold_time":None, "hold_dt":None, "hold_name":None, "hold_address":None, "hold_phone":None})
                        continue
                    await send_text(ws, MSG["please_yes_no"]); continue

                # Hard gate: if guided flow is active, skip model fallthrough entirely
                if state["mode"] in {"booking", "optout"} or state["need"] is not None:
                    if state["mode"] == "booking":
                        if not state["hold_name"]:
                            state["need"] = "name"
                            state["capture_next"] = "name"
                            state["name_collector"] = NameCollector()
                            when_say = when_phrase(state["hold_dt"], lang)
                            await send_text(ws, MSG["ask_name"].format(when=when_say))
                            continue

                        if not state["hold_address"]:
                            await send_text(ws, MESSAGES[lang]["ask_address"]) ; continue
                        if not caller_number and not state["hold_phone"]:
                            await send_text(ws, MESSAGES[lang]["ask_phone"]) ; continue
                        when_say = when_phrase(datetime.combine(state["hold_date"], state["hold_time"], tzinfo=TZ), lang)
                        await send_text(ws, MESSAGES[lang]["confirm_booking"].format(name=state["hold_name"], addr=state["hold_address"], when=when_say)) ; state["need"] = "confirm" ; continue
                    if state["mode"] == "optout":
                        if not state["hold_name"]:
                            await send_text(ws, MESSAGES[lang]["opt_start"]) ; continue
                        if not state["hold_address"]:
                            await send_text(ws, MESSAGES[lang]["opt_addr"]) ; continue
                        if not caller_number and not state["hold_phone"]:
                            await send_text(ws, MESSAGES[lang]["opt_phone"]) ; continue
                        ph = state["hold_phone"] or caller_number
                        await send_text(ws, MESSAGES[lang]["opt_confirm"].format(name=state["hold_name"], addr=state["hold_address"], phone=ph)) ; state["need"] = "confirm" ; continue

                # Normal chat fallthrough (language-aware)
                try:
                    resp = client.responses.create(
                        model="gpt-4o-mini",
                        input=[
                            {"role":"system","content": (SYSTEM_PROMPT_EN if lang=='en' else SYSTEM_PROMPT_ES)},
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
                print(f"FLOW(after): mode={state['mode']} need={state['need']}", flush=True)
                history.append({"role": "user", "content": user_text})
                history.append({"role": "assistant", "content": ai_text})
                await send_text(ws, ai_text)
                continue

            if mtype == "interrupt":
                print("Interrupted:", msg.get("utteranceUntilInterrupt", ""), flush=True)
                if state.get("need") == "name":
                    state["capture_next"] = "name"
                continue

            if mtype == "error":
                print("ConversationRelay error:", msg.get("description"), flush=True)
                continue

    except WebSocketDisconnect:
        print("ConversationRelay: disconnected", flush=True)


def _name_tokens(s: str):
    s = (s or "").strip()
    s = re.sub(r"[^\wÁÉÍÓÚÜÑáéíóúüñ'\-\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    toks = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ'\-]{0,60}", s)
    return [t for t in toks if len(t) >= 2]
