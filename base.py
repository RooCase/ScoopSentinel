"""
PyBot — Litter-Robot 4 SMS alert script.

Polls a Litter-Robot 4 via the pylitterbot library, then sends SMS alerts
through Textbelt when the waste tray or litter sand level crosses a threshold.
Includes message throttling, quiet hours, a once-daily morning digest, and a
CSV log of every reading.

Intended to be run on a regular interval via cron (e.g. every 15–30 minutes).
"""
import asyncio
import csv
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import pylitterbot

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
# All time comparisons use Central time so quiet-hours logic is timezone-aware.
CENTRAL  = ZoneInfo("America/Chicago")

# ---------------------------------------------------------------------------
# Waste-tray (DFI) thresholds — values are percentages (0–100).
# BOXcutoff1: urgent, repeat every hour.
# BOXcutoff2: advisory, repeat every four hours.
# ---------------------------------------------------------------------------
BOXcutoff1 = 90
BOXcutoff2 = 70

# ---------------------------------------------------------------------------
# Litter-level thresholds — values are fractions of the optimal fill (0.0–1.0).
# The robot reports raw mm; main() normalises to this 0–1 scale by dividing by
# the optimal fill height (450 mm from the API's "optimalLitterLevel" field).
# litterLevel1: urgent, repeat every hour.
# litterLevel2: advisory, repeat every four hours.
# ---------------------------------------------------------------------------
litterLevel1 = 0.2
litterLevel2 = 0.6

def container_level(level: float) -> str:
    """Return the alert message for the waste tray, or '' if below threshold.

    Args:
        level: Waste-tray fill percentage (0–100) from the DFILevelPercent field.

    Returns:
        A human-readable SMS string, or '' when no alert is warranted.
    """
    if level >= BOXcutoff1:
        return f"ALERT: Litter Robot is almost full at {level:.1f}%. Please replace ASAP."
    elif level >= BOXcutoff2:
        return f"Hey, Litter Robot is at {level:.1f}%! Please consider emptying soon."
    return ""

def litter_level_message(litterlevel: float) -> str:
    """Return a status message for the litter sand level.

    Always returns a non-empty string; callers must also check should_send()
    to decide whether the 'fine' message should actually be transmitted.

    Args:
        litterlevel: Sand level as a fraction of optimal fill (0.0–1.0).
                     Computed in main() as raw_mm / 450.
    """
    aspercentage = litterlevel * 100
    if litterlevel <= litterLevel1:
        return f"ALERT: Litter needs refill URGENTLY: current level is {aspercentage:.1f}%"
    if litterlevel <= litterLevel2:
        return f"Litter needs refill: current level is {aspercentage:.1f}%"
    else:
        return f"Litter level is fine as of now, at {aspercentage:.1f}%"



def should_send(level: float, msg_type: str) -> bool:
    """Return True if a message of msg_type should be sent given throttle rules and time window.

    Two independent gates must both pass:
      1. Quiet hours — only allows sending between 8 AM and 11 PM Central.
      2. Throttle window — prevents repeat messages within a cooldown period
         (1 hour for urgent levels, 4 hours for advisory levels).

    The throttle window is determined by looking up the most recent row in
    LOG_FILE where sent=True and type=msg_type, then comparing its timestamp
    against the current time.

    Args:
        level:    For "container": DFILevelPercent (0–100).
                  For "litter":    normalised sand level (0.0–1.0).
        msg_type: "container" or "litter".

    Returns:
        True if the message should be sent now; False otherwise.
    """
    now = datetime.now(CENTRAL)

    # Gate 1: quiet hours — no texts before 8 AM or after 11 PM.
    if not (8 <= now.hour < 23):
        return False

    # Gate 2a: determine the throttle window based on severity.
    if msg_type == "container":
        if level >= BOXcutoff1:
            window = timedelta(hours=1)
        elif level >= BOXcutoff2:
            window = timedelta(hours=4)
        else:
            return False  # below both thresholds — no alert needed
    elif msg_type == "litter":
        if level <= litterLevel1:
            window = timedelta(hours=1)
        elif level <= litterLevel2:
            window = timedelta(hours=4)
        else:
            return False  # level is fine, no message needed

    # Gate 2b: check the log for the most recent sent message of this type.
    # csv.DictReader requires the file to have a header row; the header is
    # written by log_reading() only when the file is first created.
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, newline="") as f:
            rows = [
                r for r in csv.DictReader(f)
                if r["sent"] == "True" and r.get("type") == msg_type
            ]
        if rows:
            last_sent = datetime.fromisoformat(rows[-1]["timestamp"])
            if now - last_sent < window:
                return False

    return True


def should_send_morning() -> bool:
    """Return True if it's 8–9 AM Central and no morning digest has been sent today.

    Bypasses the time and dedup checks when TEST_MODE is True.
    """
    now = datetime.now(CENTRAL)
    if TEST_MODE:
        return True
    if not (8 <= now.hour < 9):
        return False
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, newline="") as f:
            rows = [
                r for r in csv.DictReader(f)
                if r["sent"] == "True" and r.get("type") == "morning"
            ]
        if rows:
            last_sent = datetime.fromisoformat(rows[-1]["timestamp"])
            if last_sent.date() == now.date():
                return False
    return True


def morning_digest(dataDump: dict, robot_name: str, pets: list) -> str:
    """Build the once-daily morning status SMS.

    Includes waste tray %, litter level %, laser cleanliness, lifetime scoops
    saved, and — if pets are registered — each pet's last recorded weight and
    visit count over the past 24 hours.

    Args:
        dataDump:   robot.to_dict() output (raw API fields).
        robot_name: Display name of the robot (robot.name).
        pets:       account.pets list; may be empty if none are registered.
                    Each pet object is expected to have .name, .weight, and
                    .get_visits_since(datetime) from the pylitterbot library.

    Returns:
        A multiline string ready to be sent as an SMS.
    """
    tray        = dataDump["DFILevelPercent"]
    # litterLevel is a raw sensor reading in mm; 450 mm is the optimal fill
    # height reported by the robot as "optimalLitterLevel".
    litter_pct  = (float(dataDump["litterLevel"]) / 450) * 100
    laser_dirty = dataDump.get("isLaserDirty", False)
    scoops      = dataDump.get("scoopsSavedCount", 0)

    lines = [
        f"Good morning! Daily digest for {robot_name}:",
        f"Waste receptacle: {tray:.1f}%",
        f"Litter level: {litter_pct:.1f}%",
        f"Laser dirty: {'Yes' if laser_dirty else 'No'}",
        f"Scoops saved: {scoops}",
    ]

    if pets:
        lines.append("\nPets:")
        since = datetime.now(CENTRAL) - timedelta(hours=24)
        for pet in pets:
            visits = pet.get_visits_since(since)
            weight = f"{pet.weight:.1f} lbs" if pet.weight else "unknown"
            lines.append(
                f"  {pet.name} — "
                f"Last weight: {weight}, {visits} visit(s) in last 24h"
            )

    return "\n".join(lines)


LOG_FIELDNAMES = ["timestamp", "type", "level", "sent"]


def _ensure_log_header() -> None:
    """Guarantee LOG_FILE has the correct header row before any reads or writes.

    Called once at the start of main() so that both should_send() and
    log_reading() can safely use csv.DictReader / DictWriter without hitting a
    KeyError on files created before header-writing was added.

    If the file does not exist yet, this is a no-op; log_reading() will create
    it with a header on first write.
    """
    if not os.path.exists(LOG_FILE):
        return
    with open(LOG_FILE, newline="") as f:
        first_line = f.readline().strip()
    if first_line != ",".join(LOG_FIELDNAMES):
        # Prepend the header to the existing headerless file.
        with open(LOG_FILE, newline="") as f:
            body = f.read()
        with open(LOG_FILE, "w", newline="") as f:
            f.write(",".join(LOG_FIELDNAMES) + "\n" + body)


def log_reading(level: float, sent: bool, msg_type: str) -> None:
    """Append one sensor reading to the CSV log (litter_log.csv).

    The log is the source of truth for throttle checks in should_send() and
    should_send_morning(). Every invocation of main() writes at least two rows
    (one container, one litter) regardless of whether a text was sent, giving a
    continuous history of sensor readings.

    CSV columns: timestamp (ISO-8601 with tz), type, level, sent (True/False).

    Assumes _ensure_log_header() has already been called this run, so the file
    either doesn't exist yet or already has the correct header.

    Args:
        level:    Numeric reading to record. Container values are stored as-is
                  (0–100 %); litter values are stored as percentage (0–100),
                  i.e. the caller multiplies by 100 before passing in.
        sent:     Whether an SMS was dispatched for this reading.
        msg_type: "container", "litter", or "morning".
    """
    now = datetime.now(CENTRAL)
    write_header = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow({
            "timestamp": now.isoformat(),
            "type": msg_type,
            "level": f"{level:.1f}",
            "sent": sent,
        })


async def send_text(session: aiohttp.ClientSession, message: str) -> None:
    """Send an SMS to all configured numbers via Textbelt."""
    for number in phones:
        resp = await session.post(
            "https://textbelt.com/text",
            data={"number": number, "message": message, "key": textbelt_key},
        )
        result = await resp.json()
        if result.get("success"):
            print(f"Text sent to {number}.")
        else:
            print(f"Text to {number} failed: {result.get('error') or result.get('message')}")


async def main() -> None:
    """Connect to the Litter-Robot account, poll sensors, and dispatch alerts.

    Flow per robot (LitterRobot4 only; other robot types are skipped):
      1. Refresh live data from the cloud API.
      2. Evaluate waste-tray and litter-level thresholds.
      3. Collect any alert messages into `parts`; log every reading.
      4. If both alert types fire, combine them into one text to avoid two
         back-to-back messages; otherwise send the single alert on its own.
      5. Independently check whether a morning digest is due and send it.

    account.disconnect() is always called — even on error — via the finally
    block, so the API session is not leaked.
    """
    # Repair a headerless log before any CSV reads occur this run.
    _ensure_log_header()

    account = pylitterbot.Account()

    try:
        await account.connect(
            username=username, password=password, load_robots=True, load_pets=True
        )

        async with aiohttp.ClientSession() as session:
            for robot in account.robots:
                # Only LitterRobot4 exposes the fields this script relies on.
                if isinstance(robot, pylitterbot.LitterRobot4):
                    await robot.refresh()
                    dataDump = robot.to_dict()

                    traylevel   = dataDump["DFILevelPercent"]
                    # Normalise litter from raw mm to a 0.0–1.0 fraction so it
                    # can be compared directly against the litterLevel constants.
                    litterlevel = float(dataDump["litterLevel"]) / 450

                    # Accumulate alert strings; sent as one combined text if both fire.
                    parts = []

                    container_msg = container_level(traylevel)
                    if container_msg and should_send(traylevel, "container"):
                        parts.append(container_msg)
                        log_reading(traylevel, sent=True, msg_type="container")
                        print("Container message queued.")
                    else:
                        reason = "below threshold" if not container_msg else "throttled or outside hours"
                        log_reading(traylevel, sent=False, msg_type="container")
                        print(f"Container: no message ({reason}).")

                    litter_msg = litter_level_message(litterlevel)
                    if litter_msg and should_send(litterlevel, "litter"):
                        parts.append(litter_msg)
                        # Store as percentage (0–100) to keep log values consistent.
                        log_reading(litterlevel * 100, sent=True, msg_type="litter")
                        print("Litter message queued.")
                    else:
                        reason = "fine" if litterlevel > litterLevel2 else "throttled or outside hours"
                        log_reading(litterlevel * 100, sent=False, msg_type="litter")
                        print(f"Litter: no message ({reason}).")

                    # Send alerts — combined into one SMS when both types fired.
                    if len(parts) > 1:
                        combined = f"Litter Robot update for {robot.name}:\n" + "\n".join(parts)
                        await send_text(session, combined)
                    elif parts:
                        await send_text(session, parts[0])
                    else:
                        print("No message sent.")

                    # Morning digest is independent of the alert path above.
                    if should_send_morning():
                        await send_text(session, morning_digest(dataDump, robot.name, account.pets))
                        log_reading(traylevel, sent=True, msg_type="morning")
                        print("Morning digest sent.")

    finally:
        await account.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
