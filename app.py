from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse
from openai import OpenAI
from dotenv import load_dotenv
import os
import time

import re
from collections import defaultdict

# simple per-call memory (lives only for the process life)
PENDING_UTTERANCE = {}

load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")
assistant_id = os.getenv("CHLOE_ASSISTANT_ID")
client = OpenAI(api_key=api_key)

app = Flask(__name__)

@app.route("/voice", methods=["POST"])
def voice():
    r = VoiceResponse()
    # Faster recognition + let caller interrupt prompts
    gather = r.gather(
        input="speech",
        action="/process",
        method="POST",
        speechTimeout="1",
        language="en-US",
        bargeIn="true"
    )
    # brief, welcoming prompt (shorter = less delay to next turn)
    gather.say("Hi, this is Chloe from Foreclosure Relief Group. How can I help you today?",
               voice="Polly.Joanna", language="en-US")
    return Response(str(r), mimetype="text/xml")


@app.route("/process", methods=["POST"])
def process():
    user_input = request.form.get("SpeechResult", "")
    call_sid   = request.form.get("CallSid", "")

    if not user_input:
        r = VoiceResponse()
        r.say("Sorry, I didn't catch that. Please go ahead.",
              voice="Polly.Joanna", language="en-US")
        # re-enter gather quickly
        gather = r.gather(
            input="speech",
            action="/process",
            method="POST",
            speechTimeout="1",
            language="en-US",
            bargeIn="true"
        )
        return Response(str(r), mimetype="text/xml")

    # store the text by CallSid, then respond immediately
    PENDING_UTTERANCE[call_sid] = user_input

    r = VoiceResponse()
    # speak right away while we compute the real answer on /answer
    r.say("Got it. One moment while I check that.", voice="Polly.Joanna", language="en-US")
    r.redirect("/answer", method="POST")
    return Response(str(r), mimetype="text/xml")

@app.route("/answer", methods=["POST"])
def answer():
    call_sid = request.form.get("CallSid", "")
    user_input = PENDING_UTTERANCE.pop(call_sid, "")

    # safety net
    if not user_input:
        r = VoiceResponse()
        r.say("Thanks for your patience. Please tell me your question once more.",
              voice="Polly.Joanna", language="en-US")
        g = r.gather(
            input="speech",
            action="/process",
            method="POST",
            speechTimeout="1",
            language="en-US",
            bargeIn="true"
        )
        return Response(str(r), mimetype="text/xml")

    # --- run the Assistant (same assistant_id you already set) ---
    thread = client.beta.threads.create()
    client.beta.threads.messages.create(
        thread_id=thread.id,
        role="user",
        content=user_input
    )
    run = client.beta.threads.runs.create(
        thread_id=thread.id,
        assistant_id=assistant_id,
        # keep responses short & warm without changing your dashboard settings
        instructions=(
            "You are Chloe. Be warm and concise. "
            "Answer in 2–3 short sentences. Avoid filler. "
            "If the caller needs more detail, stop and offer to continue."
        )
    )
    # poll for completion
    status = run.status
    while status not in ("completed", "failed", "cancelled"):
        time.sleep(0.8)
        status = client.beta.threads.runs.retrieve(thread_id=thread.id, run_id=run.id).status

    if status != "completed":
        speak = "Sorry, something hiccupped. Could you repeat that?"
    else:
        msg = client.beta.threads.messages.list(thread_id=thread.id, order="desc")
        full = msg.data[0].content[0].text.value.strip()

        # --- Trim to 2–3 sentences for snappier TTS ---
        sents = re.split(r'(?<=[.!?])\s+', full)
        short = " ".join(sents[:3]).strip()
        speak = short or "Here’s what I found."

        # end with a natural follow up
        if len(sents) > 3:
            speak += " Would you like more detail?"

    # --- speak the answer and re-open the mic quickly ---
    r = VoiceResponse()
    r.say(speak, voice="Polly.Joanna", language="en-US")
    g = r.gather(
        input="speech",
        action="/process",
        method="POST",
        speechTimeout="1",
        language="en-US",
        bargeIn="true"
    )
    g.say("Anything else I can help with?",
          voice="Polly.Joanna", language="en-US")
    return Response(str(r), mimetype="text/xml")

# --- Root: GET shows a simple message; POST redirects Twilio to /voice ---

@app.route("/", methods=["GET"])
def index():
    # Simple landing page so visiting the root doesn’t 404
    return "Chloe voice agent is running. POST /voice from Twilio.", 200

@app.route("/", methods=["POST"])
def root_redirect_to_voice():
    r = VoiceResponse()
    # If Twilio posts to root by mistake, send it to /voice
    r.redirect("/voice", method="POST")
    return Response(str(r), mimetype="text/xml")

@app.route("/health", methods=["GET"])
def health():
    # Render will try GETs (and you can use this for uptime checks)
    return {"status": "ok"}, 200


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
