# app.py — COMPLETE FILE (replace your current file)
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse
from dotenv import load_dotenv
from openai import OpenAI
import os

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
VECTOR_STORE_ID = os.getenv("CHLOE_VECTOR_STORE_ID") or os.getenv("VECTOR_STORE_ID")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # swap to gpt-4o if you want richer answers

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is missing. Set it in Render Environment and/or .env.")

client = OpenAI(api_key=OPENAI_API_KEY)
app = Flask(__name__)

# ----------- Twilio entrypoints -----------
@app.route("/voice", methods=["POST"])
def voice():
    r = VoiceResponse()
    # in /voice route
    gather = r.gather(
        input="speech",
        action="/process",
        method="POST",
        speechTimeout="2",             # was "1"
        language="en-US",
        bargeIn="true",
        speechModel="phone_call",      # better for calls
        hints="foreclosure, pre-foreclosure, short sale, loan modification, forbearance, "
            "notice of default, auction date, reinstatement, repayment plan, deed in lieu"
    )
    gather.say("Hi, this is Chloe from Foreclosure Relief Group. How can I help?")
    return Response(str(r), mimetype="text/xml")

@app.route("/process", methods=["POST"])
def process():
    user_input = (request.form.get("SpeechResult") or "").strip()
    r = VoiceResponse()

    if not user_input:
        r.say("Sorry, I didn’t catch that. Could you repeat?")
        r.redirect("/voice")
        return Response(str(r), mimetype="text/xml")

    # Small acknowledgement to feel responsive
    r.say("Got it. One moment while I check that.")

    # Build Responses call. NOTE: no attachments. Use file_search + vector_store_ids.
    tools = []
    if VECTOR_STORE_ID:
        tools = [{
            "type": "file_search",
            "vector_store_ids": [VECTOR_STORE_ID],
        }]

    try:
        resp = client.responses.create(
            model=MODEL,  # e.g., gpt-4o-mini (fast) or gpt-4o (richer)
            instructions=(
                "You are Chloe, a calm, warm, professional phone assistant for the Foreclosure Relief Group. "
                "First, acknowledge briefly (e.g., 'Got it — one moment.'). "
                "Answer using the knowledge base for foreclosure topics. "
                "Keep replies under 3 short sentences. Avoid filler and repetition. "
                "If unsure or outside scope, say so and offer to collect info or schedule."
            ),
            input=[{"role": "user", "content": user_input}],
            tools=[{
                "type": "file_search",
                "vector_store_ids": [VECTOR_STORE_ID] if VECTOR_STORE_ID else []
            }],
            temperature=0.2,
            max_output_tokens=200,
        )

        # Prefer the helper; fall back if SDK shape differs
        ai_text = getattr(resp, "output_text", None)
        if not ai_text:
            # Defensive fallback
            try:
                # Some SDK versions expose .output as a list of chunks
                ai_text = resp.output[0].content[0].text
            except Exception:
                ai_text = "Sorry, I had trouble finding an answer to that."

    except Exception:
        ai_text = "Sorry, I hit an error while looking that up."

    # Speak a short chunk, then invite follow-up to keep calls snappy
    first = ai_text.split(". ")[0].strip()
    if first:
        r.say(first + ".")
    else:
        r.say(ai_text)

    # Quick follow-up gather
    follow = r.gather(
        input="speech",
        action="/process",
        method="POST",
        speechTimeout="1",
        language="en-US"
    )
    follow.say("Would you like more detail on that?")

    return Response(str(r), mimetype="text/xml")

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

if __name__ == "__main__":
    # Local run (ignored on Render because you use `gunicorn app:app`)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
