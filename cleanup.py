import fcntl
import requests
import csv
import json
import os
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo



# ---------------------------------------------------------------------------
# Configuration — loaded from config.json at startup.
# See README for the expected keys. Never commit config.json to version control.
# ---------------------------------------------------------------------------
with open("config.json") as file:
    config = json.load(file)

username        = config["username"]
password        = config["password"]
textbelt_key    = config["textbelt_key"]
# Multiple recipients are supported: comma-separate numbers in config["phone"].
phones          = [p.strip() for p in config["phone"].split(",")]
# TEST_MODE skips quiet-hours and dedup checks so you can verify the script
# works without waiting for the right time of day. Set "test": false (or omit)
# in config.json for normal operation.
TEST_MODE       = config.get("test", False)

# Path to the append-only CSV log of every sensor reading.
LOG_FILE = "litter_log.csv"
# Lock file used to prevent overlapping cleanup.py runs.
# NOTE: base.py does not acquire this lock, so it only guards against
# concurrent cleanup invocations — not the (tiny) race where base.py opens
# LOG_FILE for append the instant before os.replace() fires.
LOCK_FILE = LOG_FILE + ".lock"
# All time comparisons use Central time so quiet-hours logic is timezone-aware.
CENTRAL  = ZoneInfo("America/Chicago")


def send_text(message: str) -> None:
    """Send an SMS to all configured numbers via Textbelt."""
    number = phones[0]
    result = requests.post(
        "https://textbelt.com/text",
        data={"number": number, "message": message, "key": textbelt_key},
    )
    data = result.json()
    if data.get("success"):
        print(f"Text sent to {number}.")
    else:
        print(f"Text to {number} failed: {data.get('error') or data.get('message')}")


def cleanup_log() -> None:
    """Remove entries older than 48 hours from litter_log.csv using a temp file.

    Uses an exclusive flock on LOCK_FILE to prevent two cleanup.py processes
    from running simultaneously. base.py does not participate in this lock, so
    there is a theoretical (microsecond-wide) race if base.py's log_reading()
    opens LOG_FILE for append at the exact moment os.replace() fires; in that
    case one row could be lost. Full safety would require base.py to also
    acquire this lock before calling log_reading().
    """
    if not os.path.exists(LOG_FILE):
        return

    cutoff = datetime.now(CENTRAL) - timedelta(hours=48)
    kept, removed = 0, 0
    tmp_path = None

    with open(LOCK_FILE, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)  # blocks until any prior cleanup finishes
        try:
            dir_name = os.path.dirname(os.path.abspath(LOG_FILE))
            with open(LOG_FILE, newline="") as infile, \
                 tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, suffix=".tmp", newline="") as tmp:
                tmp_path = tmp.name
                reader = csv.DictReader(infile)
                writer = csv.DictWriter(tmp, fieldnames=reader.fieldnames)
                writer.writeheader()
                for row in reader:
                    ts = datetime.fromisoformat(row["timestamp"])
                    if ts >= cutoff:
                        writer.writerow(row)
                        kept += 1
                    else:
                        removed += 1

            os.replace(tmp_path, LOG_FILE)
            tmp_path = None  # replace succeeded; nothing to clean up
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)  # remove orphaned temp file on failure
            fcntl.flock(lock_fd, fcntl.LOCK_UN)

    print(f"Log cleanup: removed {removed} old entries, kept {kept}.")


def main():
    cleanup_log()

    request = requests.get("https://textbelt.com/quota/" + str(textbelt_key))
    response = request.json()

    if response["quotaRemaining"] < 50:
        send_text("Just a heads up, you're running low on text tokens. There are currently " + str(response["quotaRemaining"] - 1) + " tokens.")
    else:
        print("No need to send a text")
        
    
main()
        