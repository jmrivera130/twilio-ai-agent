# create_vector_store.py
import os, glob, sys
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    print("ERROR: OPENAI_API_KEY missing in environment/.env")
    sys.exit(1)

client = OpenAI(api_key=api_key)

# 1) Create a vector store (server will handle chunking/embedding)
vs = client.vector_stores.create(name="Chloe-KB")
print(f"Created vector store: {vs.id}")

# 2) Collect local files
file_paths = glob.glob("data/*.pdf") + glob.glob("data/*.txt")
if not file_paths:
    print("ERROR: No files found in ./data. Add your PDFs/TXTs there and rerun.")
    sys.exit(1)

# 3) Upload files to OpenAI Files
file_ids = []
for path in file_paths:
    with open(path, "rb") as f:
        up = client.files.create(file=f, purpose="assistants")
        print(f"Uploaded {os.path.basename(path)} → {up.id}")
        file_ids.append(up.id)

# 4) Attach those files to the vector store (batch & poll until indexed)
batch = client.vector_stores.file_batches.create_and_poll(
    vector_store_id=vs.id,
    file_ids=file_ids
)

print("Vector store ready.")
print("VECTOR_STORE_ID:", vs.id)
print("Batch status:", batch.status)
print("File counts:", batch.file_counts)
