# create_vector_store.py  (RUN LOCALLY ONCE)
import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# 1) Create a new vector store on OpenAI
store = client.vector_stores.create(name="Chloe Knowledge Base")
print("Vector store ID:", store.id)

# 2) Upload all PDFs in /data into the store
data_dir = "data"
files = [os.path.join(data_dir, f) for f in os.listdir(data_dir) if f.lower().endswith(".pdf")]
if not files:
    print("No PDFs found in ./data. Add your PDFs there and re-run.")
    raise SystemExit(1)

for path in files:
    up = client.files.create(file=open(path, "rb"), purpose="assistants")
    client.vector_stores.files.create(vector_store_id=store.id, file_id=up.id)
    print(f"Uploaded {os.path.basename(path)} → {up.id}")

print("\n✅ Done. Copy this VECTOR_STORE_ID into your Render env vars:")
print("VECTOR_STORE_ID=", store.id)
