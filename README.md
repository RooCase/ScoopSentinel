# ScoopSentinel

A Raspberry Pi cron script that keeps an eye on your **Litter-Robot 4** and
texts you before things get gross. Alerts fire when the waste tray fills up or
the litter sand runs low, with smart throttling so you're not spammed. Every
morning it sends a digest so you always know what the cat situation is.

---

## Features

- **Waste-tray alerts** — texts at 70 % (advisory) and 90 % (urgent)
- **Litter-level alerts** — texts at 60 % remaining (advisory) and 20 % (urgent)
- **Smart throttling** — urgent alerts re-send after 1 hour, advisory after 4
- **Quiet hours** — no texts before 8 AM or after 11 PM Central
- **Morning digest** — once-daily 8 AM summary: tray %, litter %, laser status, scoops saved, and per-cat visit counts
- **Multi-recipient** — comma-separate numbers in config to text the whole household
- **Append-only CSV log** — full history of every sensor reading and whether a text was sent
- **Automatic log cleanup** — a companion script trims entries older than 48 hours and alerts you when Textbelt quota runs low

---

## Requirements

- Python 3.11+
- A Litter-Robot 4 connected to the Whisker app
- A [Textbelt](https://textbelt.com) API key
- Dependencies:

```bash
pip install pylitterbot aiohttp
```

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/RooCase/ScoopSentinel
cd ScoopSentinel
```

### 2. Create `config.json`

```json
{
    "username": "you@example.com",
    "password": "your-whisker-password",
    "textbelt_key": "your-textbelt-key",
    "phone": "6125550000",
    "test": false
}
```

| Key | Description |
|-----|-------------|
| `username` | Whisker / Litter-Robot account email |
| `password` | Whisker / Litter-Robot account password |
| `textbelt_key` | Textbelt API key — get one at [textbelt.com](https://textbelt.com) |
| `phone` | Recipient number(s). Comma-separate for multiple: `"6125550000,6125551111"` |
| `test` | `true` skips quiet-hours and dedup checks during development |

> **`config.json` is in `.gitignore` — never commit it.**

### 3. Schedule with cron

Run every 15 minutes, all day (the script enforces quiet hours itself):

```
*/15 * * * * cd /home/pi/ScoopSentinel && /home/pi/ScoopSentinel/venv/bin/python base.py >> /tmp/scoopsentinel.log 2>&1
```

Run `cleanup.py` twice daily to trim the log and check Textbelt quota:

```
17 8  * * * cd /home/pi/ScoopSentinel && /home/pi/ScoopSentinel/venv/bin/python cleanup.py >> /tmp/scoopsentinel-cleanup.log 2>&1
32 16 * * * cd /home/pi/ScoopSentinel && /home/pi/ScoopSentinel/venv/bin/python cleanup.py >> /tmp/scoopsentinel-cleanup.log 2>&1
```

---

## Thresholds

All alert thresholds are constants at the top of [base.py](base.py):

```python
BOXcutoff1 = 90    # waste tray % — urgent
BOXcutoff2 = 70    # waste tray % — advisory

litterLevel1 = 0.2  # sand level — urgent  (20 %)
litterLevel2 = 0.6  # sand level — advisory (60 %)
```

---

## Log file

`litter_log.csv` is created automatically and has four columns:

| Column | Description |
|--------|-------------|
| `timestamp` | ISO-8601 with timezone |
| `type` | `container`, `litter`, or `morning` |
| `level` | Numeric reading as a percentage (0–100) |
| `sent` | `True` if an SMS was sent for this row |

The throttle logic reads this file to determine when the last alert went out —
don't delete it between runs unless you want the cooldown to reset.

---

## cleanup.py

`cleanup.py` is a companion maintenance script that runs independently of `base.py`. Each time it runs it:

1. **Trims the log** — removes any rows from `litter_log.csv` older than 48 hours while preserving all headers and newer data. The rewrite uses a temp file + atomic `os.replace()` so the log is never left in a partial state.
2. **Checks Textbelt quota** — queries the Textbelt API for your remaining SMS balance and texts you if it falls below 50 credits.

It uses an exclusive file lock (`litter_log.csv.lock`) to prevent two simultaneous invocations from racing each other. `base.py` does not participate in this lock, so the two scripts are safe to run concurrently — the only theoretical overlap is the microsecond window in which `base.py` could open the log for append at the exact moment the atomic replacement fires, which would cause at most one row to be lost.
