from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse
from openai import OpenAI
from dotenv import load_dotenv
import os
import time

load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")
assistant_id = os.getenv("CHLOE_ASSISTANT_ID")
client = OpenAI(api_key=api_key)

app = Flask(__name__)

@app.route("/voice", methods=["POST"])
def voice():
    response = VoiceResponse()
    gather = response.gather(
        input="speech",
        action="/process",
        method="POST",
        speechTimeout="auto",
        language="en-US"
    )
    gather.say("Hi, this is Chloe from Foreclosure Relief Group.  ")
    gather.say("How can I help you today?")
    return Response(str(response), mimetype="text/xml")

@app.route("/process", methods=["POST"])
def process():
    user_input = request.form.get("SpeechResult", "")
    if not user_input:
        resp = VoiceResponse()
        resp.say("Sorry, I didn’t catch that.")
        resp.redirect("/voice")
        return Response(str(resp), mimetype="text/xml")

    resp = VoiceResponse()
    resp.say("Alright, just a second...")

    thread = client.beta.threads.create()
    client.beta.threads.messages.create(
        thread_id=thread.id,
        role="user",
        content=user_input,
        attachments=[
            {"file_id": "file-BGiRXdsiJhHh4NzTxzCAeW", "tools": [{"type": "file_search"}]},
            {"file_id": "file-7zPQWPh7tCCmteBDuFX93z", "tools": [{"type": "file_search"}]}
        ],
    )

    run = client.beta.threads.runs.create(
        thread_id=thread.id,
        assistant_id=assistant_id
    )

    while True:
        status = client.beta.threads.runs.retrieve(thread_id=thread.id, run_id=run.id).status
        if status == "completed":
            break
        if status in ["failed", "cancelled"]:
            resp.say("Sorry, something went wrong.")
            return Response(str(resp), mimetype="text/xml")
        time.sleep(1)

    msg = client.beta.threads.messages.list(thread_id=thread.id, order="desc")
    ai_reply = msg.data[0].content[0].text.value.strip()
    resp.say(ai_reply)

    gather = resp.gather(
        input="speech",
        action="/process",
        method="POST",
        speechTimeout="auto",
        language="en-US"
    )
    gather.say("Is there anything else I can help with?")
    return Response(str(resp), mimetype="text/xml")

@app.route("/", methods=["GET"])
def index():
    # Simple landing page so visiting the root doesn’t 404
    return "Chloe voice agent is running. POST /voice from Twilio.", 200

@app.route("/health", methods=["GET"])
def health():
    # Render will try GETs (and you can use this for uptime checks)
    return {"status": "ok"}, 200


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
