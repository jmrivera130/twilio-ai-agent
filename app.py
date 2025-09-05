# app.py  (COMPLETE FILE - REPLACE YOURS)
import os, time
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse
from openai import OpenAI

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
VECTOR_STORE_ID = os.environ.get("VECTOR_STORE_ID")

client = OpenAI(api_key=OPENAI_API_KEY)
app = Flask(__name__)

# ----------- Twilio entrypoints -----------
@app.route("/voice", methods=["POST"])
def voice():
    r = VoiceResponse()
    # Faster turn-taking: short timeout; keep prompt brief
    gather = r.gather(
        input="speech",
        action="/process",
        method="POST",
        speechTimeout="1",
        language="en-US"
    )
    gather.say("Hi, this is Chloe from Foreclosure Relief Group. How can I help you today?")
    return Response(str(r), mimetype="text/xml")

@app.route("/process", methods=["POST"])
def process():
    user_input = request.form.get("SpeechResult", "").strip()
    r = VoiceResponse()

    if not user_input:
        r.say("Sorry, I didn't catch that.")
        r.redirect("/voice")
        return Response(str(r), mimetype="text/xml")

    # Acknowledge quickly so caller knows we’re working
    r.say("Got it. One moment while I check that.")

    # ---- Responses API with File Search (vector store) ----
    system_style = (
        "You are Chloe, a calm, warm phone assistant for Foreclosure Relief Group. "
        "Be concise (2–4 sentences). If unsure, say so and offer to connect to a specialist. "
        "Only use information from the company knowledge base unless the question is general."
    )

    # Ask the model, letting it search your vector store
    resp = client.responses.create(
        model="gpt-4o-mini",                 # faster than full 4o; upgrade if you want
        system=system_style,
        input=user_input,
        tools=[{
            "type": "file_search",
            "vector_store_ids": [VECTOR_STORE_ID],
        }],
        max_output_tokens=220
    )

    # Robustly extract text (helper exists in new SDKs)
    ai_text = getattr(resp, "output_text", None)
    if not ai_text:
        # Fallback for older SDKs
        try:
            ai_text = resp.output[0].content[0].text
        except Exception:
            ai_text = "Sorry, I had trouble answering that."

    # Speak the answer in shorter chunks
    first_sentence = ai_text.split(". ")[0].strip()
    r.say(first_sentence + ".")
    if len(ai_text) > len(first_sentence) + 5:
        r.pause(length=0.3)
        r.say("Would you like more detail?")

        # Re-gather so caller can say “Yes” or ask another question
        gather = r.gather(
            input="speech",
            action="/process",
            method="POST",
            speechTimeout="1",
            language="en-US"
        )
        gather.say("I'm listening.")
        return Response(str(r), mimetype="text/xml")

    # Standard follow-up path
    follow = r.gather(
        input="speech",
        action="/process",
        method="POST",
        speechTimeout="1",
        language="en-US"
    )
    follow.say("Anything else I can help with?")
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
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
