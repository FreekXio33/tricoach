#!/usr/bin/env python3
"""
sync_garmin.py — Garmin Connect → Supabase daily sync
Fetches Body Battery, HRV, sleep score/duration, stress, SpO2 for
the previous day and upserts into the garmin_daily Supabase table.

Usage:
    python3 sync_garmin.py              # sync yesterday
    python3 sync_garmin.py 2026-03-15   # sync a specific date
    python3 sync_garmin.py --days 7     # back-fill last 7 days

Setup:
    1. cp .env.example .env && fill in your credentials
    2. python3 sync_garmin.py
    3. Schedule via cron: see bottom of this file
"""

import sys
import os
import json
import logging
import argparse
from datetime import date, timedelta
from pathlib import Path

# ── third-party ──────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
except ImportError:
    sys.exit("Missing python-dotenv. Run: pip3 install python-dotenv")

try:
    import garminconnect
except ImportError:
    sys.exit("Missing garminconnect. Run: pip3 install garminconnect")

try:
    from supabase import create_client, Client
except ImportError:
    sys.exit("Missing supabase. Run: pip3 install supabase")

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sync_garmin")

# ── load .env ─────────────────────────────────────────────────────────────────
env_path = Path(__file__).parent / ".env"
load_dotenv(env_path)

GARMIN_EMAIL    = os.getenv("GARMIN_EMAIL")
GARMIN_PASSWORD = os.getenv("GARMIN_PASSWORD")
SUPABASE_URL    = os.getenv("SUPABASE_URL")
SUPABASE_KEY    = os.getenv("SUPABASE_KEY")
ATHLETE_ID      = os.getenv("ATHLETE_ID")


# ── garmin session cache (avoids MFA on every run) ───────────────────────────
TOKEN_FILE = Path.home() / ".garmin_tokens.json"


def get_garmin_client() -> garminconnect.Garmin:
    """Login to Garmin Connect, reusing cached tokens when possible."""
    client = garminconnect.Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)

    if TOKEN_FILE.exists():
        log.info("Loading cached Garmin tokens from %s", TOKEN_FILE)
        try:
            client.login(TOKEN_FILE)
            return client
        except Exception as e:
            log.warning("Cached token invalid (%s) — re-authenticating", e)

    log.info("Authenticating with Garmin Connect...")
    client.login()
    client.garth.dump(TOKEN_FILE)
    log.info("Tokens saved to %s", TOKEN_FILE)
    return client


# ── data extraction helpers ──────────────────────────────────────────────────

def safe_get(fn, *args, default=None, label=""):
    """Call fn(*args) and return default on any exception."""
    try:
        return fn(*args)
    except Exception as e:
        log.debug("Could not fetch %s: %s", label, e)
        return default


def extract_body_battery(client: garminconnect.Garmin, day: str) -> int | None:
    """Return end-of-day Body Battery level (0-100)."""
    data = safe_get(client.get_body_battery, day, label="body battery")
    if not data:
        return None
    # API returns a list of dicts with 'charged' and 'drained' keys
    if isinstance(data, list):
        for entry in reversed(data):
            val = entry.get("bodyBatteryLevel") or entry.get("charged")
            if val is not None:
                return int(val)
    if isinstance(data, dict):
        return data.get("bodyBatteryLevel") or data.get("charged")
    return None


def extract_hrv(client: garminconnect.Garmin, day: str) -> int | None:
    """Return average overnight HRV (ms)."""
    data = safe_get(client.get_hrv_data, day, label="HRV")
    if not data:
        return None
    if isinstance(data, dict):
        summary = data.get("hrvSummary") or data
        val = summary.get("lastNight") or summary.get("weeklyAvg") or summary.get("hrvValue")
        if val is not None:
            return int(val)
    return None


def extract_sleep(client: garminconnect.Garmin, day: str) -> tuple[int | None, float | None]:
    """Return (sleep_score 0-100, sleep_duration_hours)."""
    data = safe_get(client.get_sleep_data, day, label="sleep")
    if not data:
        return None, None
    summary = data.get("dailySleepDTO") or data
    score    = summary.get("sleepScores", {}).get("overall", {}).get("value") or \
               summary.get("overallSleepScore")
    duration = summary.get("sleepTimeSeconds")
    score    = int(score)           if score    else None
    duration = round(duration / 3600, 2) if duration else None
    return score, duration


def extract_stress(client: garminconnect.Garmin, day: str) -> int | None:
    """Return average stress level (0-100)."""
    data = safe_get(client.get_stress_data, day, label="stress")
    if not data:
        return None
    if isinstance(data, dict):
        val = data.get("avgStressLevel") or data.get("averageStressLevel")
        if val and val > 0:
            return int(val)
    if isinstance(data, list):
        vals = [d.get("stressLevel", 0) for d in data if d.get("stressLevel", 0) > 0]
        if vals:
            return int(sum(vals) / len(vals))
    return None


def extract_spo2(client: garminconnect.Garmin, day: str) -> float | None:
    """Return average SpO2 (%)."""
    data = safe_get(client.get_spo2_data, day, label="SpO2")
    if not data:
        return None
    if isinstance(data, dict):
        val = data.get("averageSPO2") or data.get("averageSpO2")
        if val:
            return round(float(val), 1)
        readings = data.get("spO2HourlyAverages") or []
        vals = [r.get("spo2Reading") or r.get("value", 0) for r in readings if r.get("spo2Reading") or r.get("value")]
        if vals:
            return round(sum(vals) / len(vals), 1)
    return None


# ── main sync ─────────────────────────────────────────────────────────────────

def sync_day(client: garminconnect.Garmin, sb: Client, target_date: date) -> dict:
    day = target_date.strftime("%Y-%m-%d")
    log.info("Syncing %s …", day)

    body_battery           = extract_body_battery(client, day)
    hrv                    = extract_hrv(client, day)
    sleep_score, sleep_dur = extract_sleep(client, day)
    stress                 = extract_stress(client, day)
    spo2                   = extract_spo2(client, day)

    row = {
        "date":           day,
        "body_battery":   body_battery,
        "hrv":            hrv,
        "sleep_score":    sleep_score,
        "sleep_duration": sleep_dur,
        "stress":         stress,
        "spo2":           spo2,
    }
    if ATHLETE_ID:
        row["athlete_id"] = int(ATHLETE_ID)

    # Filter out None values to avoid overwriting good data with nulls
    row_clean = {k: v for k, v in row.items() if v is not None}

    log.info(
        "  Body Battery=%-4s HRV=%-4s Sleep score=%-4s dur=%-5s Stress=%-4s SpO2=%s",
        body_battery, hrv, sleep_score,
        f"{sleep_dur}h" if sleep_dur else "—",
        stress, spo2
    )

    try:
        sb.table("garmin_daily").upsert(
            row_clean,
            on_conflict="date" + (",athlete_id" if ATHLETE_ID else "")
        ).execute()
        log.info("  ✓ Upserted to Supabase")
    except Exception as e:
        log.error("  Supabase upsert failed: %s", e)

    return row_clean


def main():
    # ── validate config ───────────────────────────────────────────────────────
    missing = [k for k, v in {
        "GARMIN_EMAIL": GARMIN_EMAIL,
        "GARMIN_PASSWORD": GARMIN_PASSWORD,
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_KEY": SUPABASE_KEY,
    }.items() if not v]
    if missing:
        sys.exit(f"Missing env vars: {', '.join(missing)}. Check your .env file.")

    # ── args ──────────────────────────────────────────────────────────────────
    parser = argparse.ArgumentParser(description="Sync Garmin Connect data to Supabase")
    parser.add_argument("date", nargs="?", help="Target date YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--days", type=int, default=1, help="Number of past days to back-fill")
    args = parser.parse_args()

    dates: list[date] = []
    if args.date:
        dates = [date.fromisoformat(args.date)]
    else:
        today = date.today()
        dates = [today - timedelta(days=i) for i in range(1, args.days + 1)]

    # ── connect ───────────────────────────────────────────────────────────────
    client = get_garmin_client()
    sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

    # ── sync ──────────────────────────────────────────────────────────────────
    results = [sync_day(client, sb, d) for d in dates]
    log.info("Done — %d day(s) synced.", len(results))
    return results


if __name__ == "__main__":
    main()


# ── CRON SETUP ────────────────────────────────────────────────────────────────
# Run every morning at 07:00:
#
#   crontab -e
#   0 7 * * * /usr/bin/python3 /Users/Alex/Desktop/tricoach/sync_garmin.py >> /tmp/sync_garmin.log 2>&1
#
# Or with a venv python:
#   0 7 * * * /path/to/venv/bin/python3 /Users/Alex/Desktop/tricoach/sync_garmin.py >> /tmp/sync_garmin.log 2>&1
