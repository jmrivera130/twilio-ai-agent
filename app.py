# app.py — fast-turn Voice agent with background generation + redirect
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse
from twilio.rest import Client as TwilioClient
from dotenv import load_dotenv
from openai import OpenAI
from collections import defaultdict
from urllib.parse import quote
import threading
import os
import re

load_dotenv()

# ---- Env ----
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
VECTOR_STORE_ID  = os.getenv("CHLOE_VECTOR_STORE_ID") or os.getenv("VECTOR_STORE_ID")
MODEL            = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # try "gpt-4o" later for richer answers
PUBLIC_HOST_URL  = os.getenv("PUBLIC_HOST_URL")              # e.g. https://chloe-ai-agent.onrender.com

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY missing.")
if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
    raise RuntimeError("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN missing.")

# ---- Clients ----
client_oai   = OpenAI(api_key=OPENAI_API_KEY)
client_twilio = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# ---- App & memory ----
app = Flask(__name__)
SESSIONS = defaultdict(list)   # { CallSid: [ {"role": "user"/"assistant", "content": "..."} ] }
MAX_TURNS = 8

# Simple keyword gate to avoid doing file_search on chit-chat
FORECLOSURE_KEYWORDS = [
    "foreclosure", "pre-foreclosure", "notice of default", "auction", "sale date",
    "short sale", "loan modification", "forbearance", "reinstatement", "repayment plan",
    "deed in lieu", "lis pendens"
]

def needs_file_search(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in FORECLOSURE_KEYWORDS)

# ---------- Health & root ----------
@app.route("/", methods=["GET"])
def index():
    return "Chloe voice agent is running. POST /voice from Twilio.", 200

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200

@app.route("/", methods=["POST"])
def root_redirect_to_voice():
    r = VoiceResponse()
    r.redirect("/voice", method="POST")
    return Response(str(r), mimetype="text/xml")

# ---------- Voice entry ----------
@app.route("/voice", methods=["POST"])
def voice():
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <ConversationRelay
      url="{os.environ['RELAY_WSS_URL']}"
      welcomeGreeting="Hi, I’m Chloe. How can I help?"
    />
  </Connect>
</Response>"""
    return Response(twiml, mimetype="text/xml")

# ---------- Background worker ----------
def generate_and_redirect(call_sid: str, user_input: str, base_url: str):
    """Runs in a thread: call OpenAI, then redirect the live call to /speak with the answer."""
    try:
        history = SESSIONS[call_sid][-4:]  # short context to avoid repetition/latency
        # Build tools list only if the question looks on-topic
        tools = []
        if VECTOR_STORE_ID and needs_file_search(user_input):
            tools = [{
                "type": "file_search",
                "vector_store_ids": [VECTOR_STORE_ID],
                # You can also try: "max_num_results": 3
            }]

        resp = client_oai.responses.create(
            model=MODEL,
            instructions=(
                "You are Chloe, a calm, warm, professional phone assistant for the Foreclosure Relief Group. "
                "Use the knowledge base for foreclosure topics. Keep answers to 2–3 short sentences. "
                "Avoid filler and repetition. If unsure, say so and offer to collect details or schedule."
            ),
            input=history + [{"role": "user", "content": user_input}],
            tools=tools,
            max_output_tokens=180,
            temperature=0.2,
        )

        ai_text = getattr(resp, "output_text", None)
        if not ai_text:
            try:
                ai_text = resp.output[0].content[0].text
            except Exception:
                ai_text = "Sorry, I had trouble finding an answer to that."

        ai_text = (ai_text or "").strip()

        # Save assistant turn
        SESSIONS[call_sid].append({"role": "assistant", "content": ai_text})
        if len(SESSIONS[call_sid]) > MAX_TURNS:
            SESSIONS[call_sid] = SESSIONS[call_sid][-MAX_TURNS:]

        # Redirect the live call to /speak with the answer
        safe_text = quote(ai_text[:1400])  # keep URL small
        speak_url = f"{base_url}/speak?text={safe_text}"
        client_twilio.calls(call_sid).update(url=speak_url, method="GET")

    except Exception as e:
        # On error, send a short apology
        safe_text = quote("Sorry, I hit an error while looking that up.")
        speak_url = f"{PUBLIC_HOST_URL}/speak?text={safe_text}"
        try:
            client_twilio.calls(call_sid).update(url=speak_url, method="GET")
        except Exception:
            pass

# ---------- Process user speech ----------
@app.route("/process", methods=["POST"])
def process():
    call_sid  = request.form.get("CallSid") or "unknown"
    user_input = (request.form.get("SpeechResult") or "").strip()

    r = VoiceResponse()

    # exit phrases
    lower = user_input.lower()
    if any(kw in lower for kw in [
        "that's all", "that is all", "i'm good", "im good", "no thanks", "no, thanks",
        "nope", "bye", "goodbye", "hang up"
    ]):
        r.say("Okay. Thanks for calling. Take care.")
        r.hangup()
        SESSIONS.pop(call_sid, None)
        return Response(str(r), mimetype="text/xml")

    if not user_input:
        r.say("Sorry, I didn’t catch that. Could you repeat?")
        r.redirect("/voice")
        return Response(str(r), mimetype="text/xml")

    # Save user turn
    SESSIONS[call_sid].append({"role": "user", "content": user_input})
    if len(SESSIONS[call_sid]) > MAX_TURNS:
        SESSIONS[call_sid] = SESSIONS[call_sid][-MAX_TURNS:]

    # Immediately respond so there is no dead air, then compute in background
    r.say("Got it. One moment while I check that.")
    r.pause(length=2)  # gives background thread time to compute
    # Return this TwiML quickly...
    twiml = Response(str(r), mimetype="text/xml")
    base_url = (os.getenv("PUBLIC_HOST_URL") or request.host_url).rstrip("/")

    # ...and do the model call + redirect in the background
    threading.Thread(
    target=generate_and_redirect,
    args=(call_sid, user_input, base_url),
    daemon=True
).start()
    return twiml

# ---------- Speak endpoint (Twilio will GET this after we redirect the call) ----------
@app.route("/speak", methods=["GET"])
def speak():
    text = request.args.get("text", "").strip()
    r = VoiceResponse()

    # Say only first sentence for snappiness; offer more detail if long
    first = re.split(r"(?<=[.!?])\s+", text)[0] if text else ""
    to_say = first if first else text
    if to_say and not to_say.endswith((".", "!", "?")):
        to_say += "."

    # You can set a better voice if your Twilio plan supports Polly voices:
    # r.say(to_say, voice="Polly.Joanna")  # if supported; otherwise default voice
    r.say(to_say)

    # Follow-up gather (keep it short)
    follow = r.gather(
        input="speech",
        action="/process",
        method="POST",
        speechTimeout="2",
        language="en-US",
        bargeIn="true",
        speechModel="phone_call",
        hints="yes, no, more detail, repeat, appointment, schedule, address, email, phone"
    )
    follow.say("Would you like more detail on that?")
    return Response(str(r), mimetype="text/xml")

if __name__ == "__main__":
    # Local run (Render uses `gunicorn app:app`)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
