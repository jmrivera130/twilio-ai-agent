import os
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

# Local data dirs inside the repo
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

# Load app.py by absolute path
APP_PATH = (ROOT / "app.py").as_posix()
spec = importlib.util.spec_from_file_location("app_module", APP_PATH)
app_module = importlib.util.module_from_spec(spec)
sys.modules["app_module"] = app_module
assert spec and spec.loader
spec.loader.exec_module(app_module)


def most_recent_booking() -> Optional[Tuple[str, dict]]:
    latest: Optional[Tuple[datetime, str, dict]] = None
    for p in BOOK_DIR.glob("*.jsonl"):
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in lines:
            try:
                j = json.loads(line)
            except Exception:
                continue
            if j.get("type") != "booking":
                continue
            created = j.get("created_at") or ""
            try:
                dtc = datetime.fromisoformat(created)
            except Exception:
                dtc = datetime.min
            if latest is None or dtc > latest[0]:
                latest = (dtc, p.name, j)
    if latest:
        return latest[1], latest[2]
    return None


def main():
    found = most_recent_booking()
    if not found:
        print("No booking records found; run the WS scenarios first.")
        return
    fname, rec = found
    bid = rec["id"]
    # Parse appointment date from start
    try:
        start_dt = datetime.fromisoformat(rec["start"])
    except Exception:
        print("Bad start datetime in record", rec)
        return
    day = start_dt.date().isoformat()
    print(f"Most recent booking: id={bid} date={day} file={fname}")

    client = TestClient(app_module.app)

    # Fetch CSV report
    r = client.get(f"/reports/{day}")
    print("/reports status:", r.status_code)
    disp = r.headers.get("content-disposition")
    ctype = r.headers.get("content-type")
    print("/reports headers:", disp, ctype)
    csv_text = r.text
    print("/reports preview:")
    for i, line in enumerate(csv_text.splitlines()[:5]):
        print(line)
    assert bid in csv_text, "Booking id not found in CSV"

    # Fetch ICS download
    r2 = client.get(f"/ics/{bid}.ics")
    print("/ics status:", r2.status_code)
    print("/ics content-type:", r2.headers.get("content-type"))
    ics_text = r2.text
    print("/ics preview:")
    for i, line in enumerate(ics_text.splitlines()[:10]):
        print(line)
    assert f"UID:{bid}" in ics_text or bid in ics_text, "ICS content missing UID"


if __name__ == "__main__":
    main()

