import os
import json
from datetime import datetime, timezone
from pathlib import Path

# Configure local storage inside repo to avoid OS temp paths
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
BOOK_DIR = DATA / "appointments"
REPORT_DIR = DATA / "reports"
ICS_DIR = DATA / "ics"
BOOK_DIR.mkdir(parents=True, exist_ok=True)
REPORT_DIR.mkdir(parents=True, exist_ok=True)
ICS_DIR.mkdir(parents=True, exist_ok=True)

# Minimal env required by app.py
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("RELAY_WSS_URL", "wss://local.test/relay")
os.environ["BOOK_DIR"] = str(BOOK_DIR)
os.environ["REPORT_DIR"] = str(REPORT_DIR)
os.environ["ICS_DIR"] = str(ICS_DIR)

from fastapi.testclient import TestClient
import importlib.util
import sys

# Load app.py explicitly by path to avoid module path issues
APP_PATH = (ROOT / "app.py").as_posix()
spec = importlib.util.spec_from_file_location("app_module", APP_PATH)
app_module = importlib.util.module_from_spec(spec)
sys.modules["app_module"] = app_module
assert spec and spec.loader
spec.loader.exec_module(app_module)


def recv_text(ws):
    msg = ws.receive_json()
    assert msg.get("type") == "text", f"unexpected frame: {msg}"
    return msg["token"]


def run_booking_yes_after_suggest():
    client = TestClient(app_module.app)
    outputs = []
    with client.websocket_connect("/relay") as ws:
        ws.send_json({"type": "setup", "from": "+1555010001"})
        # Trigger booking from an affirmative + keyword
        ws.send_json({"type": "prompt", "voicePrompt": "Yes, let's schedule."})
        outputs.append(recv_text(ws))  # ask for day
        ws.send_json({"type": "prompt", "voicePrompt": "Thursday at 1pm"})
        outputs.append(recv_text(ws))  # ask for name
        ws.send_json({"type": "prompt", "voicePrompt": "My name is John Smith"})
        outputs.append(recv_text(ws))  # ask for address
        ws.send_json({"type": "prompt", "voicePrompt": "123 Main St, Stockton, CA"})
        outputs.append(recv_text(ws))  # confirm read-back
        ws.send_json({"type": "prompt", "voicePrompt": "Yes"})
        outputs.append(recv_text(ws))  # booked
    return outputs


def run_booking_direct_datetime():
    client = TestClient(app_module.app)
    outputs = []
    with client.websocket_connect("/relay") as ws:
        ws.send_json({"type": "setup", "from": "+1555010002"})
        ws.send_json({"type": "prompt", "voicePrompt": "Thursday at 1pm"})
        outputs.append(recv_text(ws))  # ask for name
        ws.send_json({"type": "prompt", "voicePrompt": "This is Alice Taylor"})
        outputs.append(recv_text(ws))  # ask for address
        ws.send_json({"type": "prompt", "voicePrompt": "456 Oak Ave, Stockton CA"})
        outputs.append(recv_text(ws))  # confirm read-back
        ws.send_json({"type": "prompt", "voicePrompt": "Yes"})
        outputs.append(recv_text(ws))  # booked
    return outputs


def run_opt_out_with_phone_prompt():
    client = TestClient(app_module.app)
    outputs = []
    with client.websocket_connect("/relay") as ws:
        # Simulate missing caller ID
        ws.send_json({"type": "setup", "from": ""})
        ws.send_json({"type": "prompt", "voicePrompt": "Do not call me again."})
        outputs.append(recv_text(ws))  # ask name
        ws.send_json({"type": "prompt", "voicePrompt": "Jane Doe"})
        outputs.append(recv_text(ws))  # ask address
        ws.send_json({"type": "prompt", "voicePrompt": "789 Pine Road, Stockton CA"})
        outputs.append(recv_text(ws))  # ask phone
        ws.send_json({"type": "prompt", "voicePrompt": "510-555-9876"})
        outputs.append(recv_text(ws))  # confirm read-back
        ws.send_json({"type": "prompt", "voicePrompt": "Yes"})
        outputs.append(recv_text(ws))  # all set
    return outputs


def scan_new_records(since: datetime):
    records = []
    for p in BOOK_DIR.glob("*.jsonl"):
        try:
            if datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc) < since:
                continue
        except Exception:
            pass
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                j = json.loads(line)
                records.append((p.name, j))
            except Exception:
                continue
    return records


def main():
    start = datetime.now(timezone.utc)

    print("-- Scenario 1: Yes after suggest --")
    out1 = run_booking_yes_after_suggest()
    for o in out1:
        try:
            print(o)
        except Exception:
            print(o.encode('utf-8', 'backslashreplace'))

    print("\n-- Scenario 2: Direct date/time --")
    out2 = run_booking_direct_datetime()
    for o in out2:
        try:
            print(o)
        except Exception:
            print(o.encode('utf-8', 'backslashreplace'))

    print("\n-- Scenario 3: Opt-out with phone prompt --")
    out3 = run_opt_out_with_phone_prompt()
    for o in out3:
        try:
            print(o)
        except Exception:
            print(o.encode('utf-8', 'backslashreplace'))

    print("\n-- New records since start --")
    for fname, rec in scan_new_records(start):
        print(fname, rec)


if __name__ == "__main__":
    main()
