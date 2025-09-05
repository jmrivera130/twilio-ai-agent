from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse
from openai import OpenAI
from dotenv import load_dotenv
import os

# --- Load credentials ---
load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")
assistant_id = os.getenv("CHLOE_ASSISTANT_ID")
client = OpenAI(api_key=api_key)

app = Flask(__name__)

# --- Route for Twilio voice call ---
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
    gather.say(
        "Hello, this is Chloe from Foreclosure Relief Group. "
        "How can I help you today?",
        voice="Polly.Salli"  # Optional: if your Twilio plan allows
    )
    return Response(str(response), mimetype="text/xml")

# --- Process user's spoken question ---
@app.route("/process", methods=["POST"])
def process():
    user_input = request.form.get("SpeechResult", "")

    if not user_input:
        response = VoiceResponse()
        response.say("Sorry, I didn't catch that. Can you repeat?")
        response.redirect("/voice")
        return Response(str(response), mimetype="text/xml")

    # --- Step 1: Create a new thread for this conversation ---
    thread = client.beta.threads.create()

    # --- Step 2: Post the user message into the thread ---
    client.beta.threads.messages.create(
        thread_id=thread.id,
        role="user",
        content=user_input
    )

    # --- Step 3: Run the Assistant on that thread ---
    run = client.beta.threads.runs.create(
        thread_id=thread.id,
        assistant_id=assistant_id
    )

    # --- Step 4: Wait for completion ---
    import time
    while True:
        status = client.beta.threads.runs.retrieve(thread_id=thread.id, run_id=run.id)
        if status.status == "completed":
            break
        elif status.status == "failed":
            response = VoiceResponse()
            response.say("Sorry, I had trouble processing that.")
            return Response(str(response), mimetype="text/xml")
        time.sleep(1.5)

    # --- Step 5: Get the response from the Assistant ---
    messages = client.beta.threads.messages
