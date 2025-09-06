# app.py — COMPLETE FILE (replace your current file)
# app.py — unified, production-safe
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse
from dotenv import load_dotenv
from openai import OpenAI
from collections import defaultdict
import os
import re

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
VECTOR_STORE_ID = os.getenv("CHLOE_VECTOR_STORE_ID") or os.getenv("VECTOR_STORE_ID")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # swap to gpt-4o / gpt-5-mini if needed

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is missing. Set it in Render Environment and/or .env.")
client = OpenAI(api_key=OPENAI_API_KEY)

app = Flask(__name__)

# Simple per-call memory (in-process). Keyed by Twilio CallSid.
SESSIONS = defaultdict(list)  # { call_sid: [ {"role": "...", "content": "..."}, ... ] }
MAX_TURNS = 8  # avoid runaway loops per call

# ----------- Health & root (Render/Twilio safety) -----------
@app.route("/", methods=["GET"])
def index():
    return "Chloe voice agent is running. POST /voice from Twilio.", 200

@app.route("/", methods=["POST"])
def root_redirect_to_voice():
    r = VoiceResponse()
    r.redirect("/voice", method="POST")
    return Response(str(r), mimetype="text/xml")

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200

# ----------- Twilio entrypoints -----------
@app.route("/voice", methods=["POST"])
def voice():
    r = VoiceResponse()
    gather = r.gather(
        input="speech",
        action="/process",
        method="POST",
        speechTimeout="2",          # brief pause allowed without cutting off
        language="en-US",
        bargeIn="true",
        speechModel="phone_call",   # telephone-optimized STT
        hints="foreclosure, pre-foreclosure, short sale, loan modification, forbearance, "
              "notice of default, auction date, reinstatement, repayment plan, deed in lieu"
    )
    gather.say("Hi, this is Chloe from Foreclosure Relief Group. How can I help?")
    return Response(str(r), mimetype="text/xml")

@app.route("/process", methods=["POST"])
def process():
    call_sid = request.form.get("CallSid") or "unknown"
    user_input = (request.form.get("SpeechResult") or "").strip()

    r = VoiceResponse()

    # basic done/exit detector before anything else
    lower = user_input.lower()
    if any(kw in lower for kw in ["that's all", "that is all", "i'm good", "im good", "no thanks", "no, thanks", "nope", "bye", "goodbye", "hang up"]):
        r.say("Okay. Thanks for calling. Take care.")
        r.hangup()
        SESSIONS.pop(call_sid, None)
        return Response(str(r), mimetype="text/xml")

    if not user_input:
        r.say("Sorry, I didn’t catch that. Could you repeat?")
        r.redirect("/voice")
        return Response(str(r), mimetype="text/xml")

    # Small acknowledgement to feel responsive
    r.say("Got it. One moment while I check that.")

    # Maintain short per-call history
    history = SESSIONS[call_sid]
    history.append({"role": "user", "content": user_input})
    if len(history) > MAX_TURNS:
        history = history[-MAX_TURNS:]
        SESSIONS[call_sid] = history

    # Build Responses call — file_search via vector_store_ids (no attachments)
    tools = []
    if VECTOR_STORE_ID:
        tools = [{
            "type": "file_search",
            "vector_store_ids": [VECTOR_STORE_ID],
        }]

    try:
        resp = client.responses.create(
            model=MODEL,
            instructions=(
                "You are Chloe, a calm, warm, professional phone assistant for the Foreclosure Relief Group. "
                "Use the knowledge base to answer questions about foreclosure, pre-foreclosure, and alternatives. "
                "If something is unclear or outside scope, say so and offer to collect details or schedule. "
                "Keep replies under three short sentences. Avoid filler and repetition."
            ),
            # include short memory to reduce 'Who am I talking to?' loops
            input=history[-4:],   # last few turns only to keep latency down
            tools=tools,
            max_output_tokens=200,
            temperature=0.2,
        )

        ai_text = getattr(resp, "output_text", None)
        if not ai_text:
            # Defensive fallback for SDK variants
            try:
                ai_text = resp.output[0].content[0].text
            except Exception:
                ai_text = "Sorry, I had trouble finding an answer to that."
        ai_text = (ai_text or "").strip()

    except Exception:
        ai_text = "Sorry, I hit an error while looking that up."

    # Prevent obvious repetition with last assistant message
    last_assistant = next((m["content"] for m in reversed(history) if m["role"] == "assistant"), "")
    if last_assistant and ai_text and ai_text[:80] == last_assistant[:80]:
        ai_text = "To add to what I said, would you like me to explain options or next steps?"

    # Speak only the first sentence for snappiness; offer detail after
    first_sentence = re.split(r"(?<=[.!?])\s+", ai_text.strip())[0] if ai_text else ""
    to_say = (first_sentence + ("" if first_sentence.endswith((".", "!", "?")) else ".")) if first_sentence else ai_text
    r.say(to_say or "Sorry, I couldn’t get a response just now.")

    # Save assistant turn to memory
    history.append({"role": "assistant", "content": ai_text})
    SESSIONS[call_sid] = history

    # Follow-up with improved STT settings
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
    follow_prompt = "Would you like more detail on that?" if len(ai_text) > 220 else "Anything else I can help with?"
    follow.say(follow_prompt)

    return Response(str(r), mimetype="text/xml")

if __name__ == "__main__":
    # Local run (Render uses `gunicorn app:app`)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
