# attach_files.py
from openai import OpenAI
import os
from dotenv import load_dotenv

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
assistant_id = os.getenv("CHLOE_ASSISTANT_ID")

# Step 1: Upload PDFs from /data
pdf_dir = "./data"
file_ids = []

for fn in os.listdir(pdf_dir):
    if fn.lower().endswith(".pdf"):
        with open(os.path.join(pdf_dir, fn), "rb") as f:
            uploaded = client.files.create(file=f, purpose="assistants")
            file_ids.append(uploaded.id)
            print(f"Uploaded {fn} → {uploaded.id}")

if not file_ids:
    print("❌ No PDFs found in /data — stop and add them.")
    exit()

# Step 2: Update assistant with file_search and files
client.beta.threads.messages.create(
    thread_id=thread.id,
    role="user",
    content=user_input,
    attachments=[
        {
            "file_id": "file-BGiRXdsiJhHh4NzTxzCAeW",
            "tools": [{"type": "file_search"}]
        },
        {
            "file_id": "file-7zPQWPh7tCCmteBDuFX93z",
            "tools": [{"type": "file_search"}]
        }
    ],
)
run = client.beta.threads.runs.create(
    thread_id=thread.id,
    assistant_id=assistant_id
)

print("✅ Successfully attached files to the assistant via file_search.")
