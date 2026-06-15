import os
import requests
import json
import sys
import argparse
import pandas as pd
from datetime import datetime, date, timedelta

# ── DHIS2 connection ──────────────────────────────────────────────────
DHIS2_URL = os.getenv("DHIS2_URL")
DHIS2_USERNAME = os.getenv("DHIS2_USERNAME")
DHIS2_PASSWORD = os.getenv("DHIS2_PASSWORD")

if not DHIS2_URL or not DHIS2_USERNAME or not DHIS2_PASSWORD:
    print("DHIS2_URL, DHIS2_USERNAME, and DHIS2_PASSWORD must be set in the environment.")
    sys.exit(1)

# Normalize the base URL: strip trailing slashes and ensure it ends with /api
# (DHIS2 API endpoints live under /api; without it the server redirects to the
#  web app and responses are not JSON).
DHIS2_URL = DHIS2_URL.rstrip("/")
if not DHIS2_URL.endswith("/api"):
    DHIS2_URL = f"{DHIS2_URL}/api"
    print(f"Note: '/api' was missing from DHIS2_URL — using {DHIS2_URL}")

AUTH = (DHIS2_USERNAME, DHIS2_PASSWORD)
HEADERS = {"Content-Type": "application/json"}

# ── DHIS2 UIDs ────────────────────────────────────────────────────────
PROGRAM_UID = None
PROFILE_STAGE_UID = None
BIRTH_OUTCOME_STAGE_UID = None
BIRTH_OUTCOME_DE = None
PHONE_ATTRIBUTE_UID = None

# ── Stop rule ─────────────────────────────────────────────────────────
# A woman stops receiving messages 40 weeks after enrollment, or as soon
# as a birth-outcome value (of any kind) has been recorded — whichever
# comes first.
WEEKS_UNTIL_STOP = 40

# ── DataStore location ────────────────────────────────────────────────
DATASTORE_NAMESPACE = "sms-campaigns"
DATASTORE_KEY = "config"


# ── CLI ───────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="FHI Enable – Lifestyle SMS campaign script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python sms_script.py                       # test mode, today only\n"
            "  python sms_script.py --next-week            # preview next Mon/Wed/Fri\n"
            "  python sms_script.py --target-date 2026-03-02\n"
            "  python sms_script.py --next-week --debug    # why each TEI is in/out\n"
            "  python sms_script.py --live                 # send SMS for real\n"
        ),
    )
    p.add_argument(
        "--live", action="store_true",
        help="Send SMS via DHIS2 gateway (default: test/export only)",
    )
    p.add_argument(
        "--target-date", type=str, metavar="YYYY-MM-DD",
        help="Treat this date as 'today' for message selection",
    )
    p.add_argument(
        "--next-week", action="store_true",
        help="Preview messages for next week's Mon, Wed, and Fri",
    )
    p.add_argument(
        "--debug", action="store_true",
        help="Print a per-date breakdown of why TEIs are included/excluded",
    )
    return p.parse_args()


# ── DataStore ─────────────────────────────────────────────────────────

def fetch_campaign_config():
    """
    Fetch campaign config from the DHIS2 dataStore.

    Expected structure at  dataStore/sms-campaigns/config :
    {
      "anchorMonday": "2025-01-06",
      "campaigns": {
        "DIET":     { "dayOfWeek": 0, "messages": ["...", ...] },
        "PHYSICAL": { "dayOfWeek": 2, "messages": ["...", ...] },
        "AIR":      { "dayOfWeek": 4, "messages": ["...", ...] }
      }
    }
    """
    url = f"{DHIS2_URL}/dataStore/{DATASTORE_NAMESPACE}/{DATASTORE_KEY}"
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Fetching campaign config from dataStore …")
    # Don't follow redirects: a 30x from the API almost always means the URL is
    # wrong (pointing at the web app, not /api) or the session isn't authorized.
    resp = requests.get(url, auth=AUTH, allow_redirects=False)

    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("Location", "(no Location header)")
        print(
            f"  ✗ DHIS2 returned a redirect (HTTP {resp.status_code}) → {location}\n"
            f"    Requested: {url}\n"
            f"    This usually means DHIS2_URL points at the web app instead of the\n"
            f"    API (is '/api' present?), or the credentials aren't authorized.\n"
            f"    Check DHIS2_URL, DHIS2_USERNAME, and DHIS2_PASSWORD."
        )
        sys.exit(1)

    if resp.status_code == 404:
        print(
            f"  ✗ DataStore key '{DATASTORE_NAMESPACE}/{DATASTORE_KEY}' not found.\n"
            f"    Upload sms_datastore_config.json first — see bottom of this script."
        )
        sys.exit(1)

    resp.raise_for_status()

    try:
        config = resp.json()
    except ValueError:
        ctype = resp.headers.get("Content-Type", "unknown")
        print(
            f"  ✗ Expected JSON from {url} but got a non-JSON response\n"
            f"    (HTTP {resp.status_code}, Content-Type: {ctype}).\n"
            f"    Is DHIS2_URL correct and pointing at the API (…/api)?\n"
            f"    First 200 chars of the response:\n"
            f"    {resp.text[:200]!r}"
        )
        sys.exit(1)

    dhis2_cfg = config.get("dhis2", {})
    global PROGRAM_UID, PROFILE_STAGE_UID, BIRTH_OUTCOME_STAGE_UID, BIRTH_OUTCOME_DE, PHONE_ATTRIBUTE_UID
    PROGRAM_UID = dhis2_cfg.get("programUid", PROGRAM_UID)
    PROFILE_STAGE_UID = dhis2_cfg.get("profileStageUid", PROFILE_STAGE_UID)
    BIRTH_OUTCOME_STAGE_UID = dhis2_cfg.get("birthOutcomeStageUid", BIRTH_OUTCOME_STAGE_UID)
    BIRTH_OUTCOME_DE = dhis2_cfg.get("birthOutcomeDe", BIRTH_OUTCOME_DE)
    PHONE_ATTRIBUTE_UID = dhis2_cfg.get("phoneAttributeUid", PHONE_ATTRIBUTE_UID)

    anchor = date.fromisoformat(config["anchorMonday"])
    campaigns = {}
    for name, cfg in config["campaigns"].items():
        campaigns[name] = {
            "day_of_week": cfg["dayOfWeek"],
            "messages": cfg["messages"],
        }

    print(f"  → {len(campaigns)} campaigns loaded.")
    return anchor, campaigns


# ── Scheduling helpers ────────────────────────────────────────────────

def is_send_day(campaign, anchor, target_date):
    if target_date.weekday() != campaign["day_of_week"]:
        return False
    weeks_since_anchor = (target_date - anchor).days // 7
    return weeks_since_anchor % 2 == 0


def get_current_message(campaign, anchor, target_date):
    """
    Which message to send on `target_date`?

    1. weeks_since_anchor  = full weeks between the anchor Monday and target_date
    2. bi_weeks            = weeks_since_anchor // 2   (one tick per send-cycle)
    3. index               = bi_weeks % message_count  (wraps around → list restarts)

    All women receive the same message on a given send-day.  A woman who
    joins late simply picks up whatever message the cycle is currently on.
    """
    msgs = campaign["messages"]
    weeks_since_anchor = (target_date - anchor).days // 7
    bi_weeks = weeks_since_anchor // 2
    idx = bi_weeks % len(msgs)
    return msgs[idx], idx + 1


def get_next_week_dates():
    """Return next week's Monday, Wednesday, Friday."""
    today = date.today()
    days_until_monday = (7 - today.weekday()) % 7 or 7
    mon = today + timedelta(days=days_until_monday)
    return [mon, mon + timedelta(days=2), mon + timedelta(days=4)]


# ── DHIS2 helpers ─────────────────────────────────────────────────────

def fetch_tracked_entities():
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] Fetching TEIs from DHIS2 …")
    params = {
        "program": PROGRAM_UID,
        "fields": (
            "trackedEntityInstance,"
            "attributes[attribute,value],"
            "enrollments[enrollmentDate,events[programStage,status,dataValues[dataElement,value]]]"
        ),
        "ouMode": "ACCESSIBLE",
        "skipPaging": "true",
    }
    resp = requests.get(
        f"{DHIS2_URL}/trackedEntityInstances", auth=AUTH, params=params
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        print("Non-JSON response from DHIS2:")
        print(resp.text[:500])
        raise
    teis = data.get("trackedEntityInstances", [])
    print(f"  → {len(teis)} TEIs fetched.")
    return teis


def parse_dhis2_date(value):
    """Parse a DHIS2 date/datetime string (e.g. '2026-01-05T00:00:00.000') to a date."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def classify_tei(tei, target_date):
    """
    Decide whether a woman receives a message on `target_date`, and why not.

    Returns one of:
      • "eligible"        — has a profile event and no stop condition reached
      • "no_profile"      — no "Women's profile and history" event (entry gate)
      • "past_40_weeks"   — 40 weeks have passed since enrollment
                            (target_date >= enrollmentDate + WEEKS_UNTIL_STOP)
      • "birth_outcome"   — a Birth Outcome value (any non-empty) is recorded
                            in the birth-outcome program stage

    (The phone-number check is applied separately, in process_date.)
    """
    enrollments = tei.get("enrollments", [])
    all_events = [ev for enroll in enrollments for ev in enroll.get("events", [])]

    # Entry condition: must have a profile event.
    has_profile = any(
        ev["programStage"] == PROFILE_STAGE_UID for ev in all_events
    )
    if not has_profile:
        return "no_profile"

    # Stop condition 1 — 40 weeks since enrollment.
    enroll_dates = [
        d
        for d in (parse_dhis2_date(e.get("enrollmentDate")) for e in enrollments)
        if d is not None
    ]
    if enroll_dates:
        # Use the most recent enrollment as the start of the 40-week window.
        stop_date = max(enroll_dates) + timedelta(weeks=WEEKS_UNTIL_STOP)
        if target_date >= stop_date:
            return "past_40_weeks"

    # Stop condition 2 — a birth outcome of any value has been recorded.
    if BIRTH_OUTCOME_DE:
        for ev in all_events:
            # Only look in the birth-outcome stage when one is configured.
            if BIRTH_OUTCOME_STAGE_UID and ev.get("programStage") != BIRTH_OUTCOME_STAGE_UID:
                continue
            for dv in ev.get("dataValues", []):
                if dv.get("dataElement") == BIRTH_OUTCOME_DE:
                    value = dv.get("value")
                    if value is not None and str(value).strip() != "":
                        return "birth_outcome"

    return "eligible"


def is_eligible(tei, target_date):
    """True when the woman should receive a message on `target_date`."""
    return classify_tei(tei, target_date) == "eligible"


def print_eligibility_breakdown(target_date, teis):
    """Print a count of how TEIs are classified for `target_date`."""
    counts = {
        "will_send": 0,     # eligible AND has a phone number
        "no_phone": 0,      # eligible but no phone number
        "no_profile": 0,
        "past_40_weeks": 0,
        "birth_outcome": 0,
    }
    for tei in teis:
        status = classify_tei(tei, target_date)
        if status == "eligible":
            counts["will_send" if get_phone(tei) else "no_phone"] += 1
        else:
            counts[status] += 1

    print(f"    [debug] {len(teis)} TEIs for {target_date.isoformat()}:")
    print(f"            will send ............ {counts['will_send']}")
    print(f"            eligible, no phone ... {counts['no_phone']}")
    print(f"            no profile event ..... {counts['no_profile']}")
    print(f"            past 40 weeks ........ {counts['past_40_weeks']}")
    print(f"            birth outcome set .... {counts['birth_outcome']}")


# Human-readable status labels for the debug CSV.
STATUS_LABELS = {
    "no_profile": "NO_PROFILE",
    "past_40_weeks": "PAST_40_WEEKS",
    "birth_outcome": "BIRTH_OUTCOME",
}


def build_debug_rows(target_date, teis):
    """One row per TEI describing how it is classified for `target_date`."""
    rows = []
    for tei in teis:
        status = classify_tei(tei, target_date)
        phone = get_phone(tei)
        if status == "eligible":
            label = "WILL_SEND" if phone else "NO_PHONE"
        else:
            label = STATUS_LABELS.get(status, status.upper())

        enroll_dates = [
            d
            for d in (
                parse_dhis2_date(e.get("enrollmentDate"))
                for e in tei.get("enrollments", [])
            )
            if d is not None
        ]
        latest_enrollment = max(enroll_dates) if enroll_dates else None

        rows.append({
            "Scheduled_Date": target_date.isoformat(),
            "TEI_ID": tei.get("trackedEntityInstance"),
            "Status": label,
            "Phone": phone or "",
            "Enrollment_Date": latest_enrollment.isoformat() if latest_enrollment else "",
            "Stop_Date_40w": (
                (latest_enrollment + timedelta(weeks=WEEKS_UNTIL_STOP)).isoformat()
                if latest_enrollment else ""
            ),
        })
    return rows


def normalize_phone(value):
    if value is None:
        return None
    v = "".join(str(value).split())
    if v.startswith("00"):
        return "+" + v[2:]
    if v.startswith("+"):
        return v
    return v


def get_phone(tei):
    for attr in tei.get("attributes", []):
        if attr["attribute"] == PHONE_ATTRIBUTE_UID:
            return normalize_phone(attr["value"])
    return None


def send_sms(phone, message):
    """POST to the DHIS2 outbound SMS gateway (sends immediately)."""
    payload = {"message": message, "recipients": [phone]}
    resp = requests.post(
        f"{DHIS2_URL}/sms/outbound", auth=AUTH, headers=HEADERS, json=payload
    )
    resp.raise_for_status()
    return resp.json()


# ── Core processing ───────────────────────────────────────────────────

def log_live_sms(tei, phone, campaign, scheduled_date, message, response):
    entry = {
        "timestamp": datetime.now().isoformat(),
        "tei": tei,
        "phone": phone,
        "campaign": campaign,
        "scheduledDate": scheduled_date,
        "message": message,
        "response": response,
    }
    with open("sms_live.log", "a") as f:
        f.write(json.dumps(entry))
        f.write("\n")


def process_date(target_date, anchor, campaigns, teis, is_live):
    """Build result rows and test-mode payloads for `target_date`."""
    active = {n: c for n, c in campaigns.items() if is_send_day(c, anchor, target_date)}
    if not active:
        return [], []

    rows = []
    api_payloads = []
    for name, cfg in active.items():
        msg, seq = get_current_message(cfg, anchor, target_date)
        total = len(cfg["messages"])

        for tei in teis:
            if not is_eligible(tei, target_date):
                continue

            phone = get_phone(tei)
            if not phone:
                # Only TEIs with a phone number are considered for sending or test preview
                continue

            row = {
                "Scheduled_Date": target_date.isoformat(),
                "Day": target_date.strftime("%A"),
                "Campaign": name,
                "SMS_Sequence": f"{seq}/{total}",
                "Message": msg,
                "TEI_ID": tei["trackedEntityInstance"],
                "Phone": phone,
                "Timestamp": datetime.now().isoformat(),
            }

            if not is_live:
                row["Status"] = "TEST"
                api_payloads.append(
                    {"message": msg, "recipients": [phone]}
                )
            else:
                try:
                    resp = send_sms(phone, msg)
                    row["Status"] = "SENT"
                    log_live_sms(
                        tei["trackedEntityInstance"],
                        phone,
                        name,
                        target_date.isoformat(),
                        msg,
                        {"ok": True, "response": resp},
                    )
                except Exception as exc:
                    row["Status"] = f"FAILED: {exc}"
                    log_live_sms(
                        tei["trackedEntityInstance"],
                        phone,
                        name,
                        target_date.isoformat(),
                        msg,
                        {"ok": False, "error": str(exc)},
                    )
            rows.append(row)
    return rows, api_payloads


# ── Main ──────────────────────────────────────────────────────────────

def run():
    args = parse_args()
    is_live = args.live

    anchor, campaigns = fetch_campaign_config()
    teis = fetch_tracked_entities()

    # ── Determine target dates ────────────────────────────────────────
    if args.next_week:
        target_dates = get_next_week_dates()
        print(f"\nPreviewing next week:")
    elif args.target_date:
        target_dates = [date.fromisoformat(args.target_date)]
        print(f"\nUsing target date:")
    else:
        target_dates = [date.today()]

    # ── Process each date ─────────────────────────────────────────────
    all_results = []
    all_api_payloads = []
    all_debug_rows = []
    for td in target_dates:
        active = {n: c for n, c in campaigns.items() if is_send_day(c, anchor, td)}
        tag = ", ".join(active.keys()) if active else "—"
        print(f"  {td.strftime('%A %Y-%m-%d'):>20s} → {tag}")
        if args.debug:
            print_eligibility_breakdown(td, teis)
            all_debug_rows.extend(build_debug_rows(td, teis))
        rows, api_payloads = process_date(td, anchor, campaigns, teis, is_live)
        all_results.extend(rows)
        all_api_payloads.extend(api_payloads)

    # ── Debug roster CSV (full TEI list, written even when nothing sends) ──
    if args.debug and all_debug_rows:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        debug_csv = f"sms_debug_{ts}.csv"
        pd.DataFrame(all_debug_rows).to_csv(debug_csv, index=False)
        print(f"\n[DEBUG] Full TEI list → {debug_csv} ({len(all_debug_rows)} rows)")

    if not all_results:
        print("\nNo messages to process for the selected date(s).")
        if not args.next_week and not args.target_date:
            print("Tip: use --next-week to preview next week, "
                  "or --target-date YYYY-MM-DD to pick a date.")
        return

    # ── Export / logging ───────────────────────────────────────────────
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    if is_live:
        label = "LIVE"
        print(f"\n[{label}] {len(all_results)} messages processed.")
        print("  Log:   sms_live.log")
        return

    prefix = "sms_test"
    csv_path = f"{prefix}_{ts}.csv"
    json_path = f"{prefix}_{ts}.json"

    df = pd.DataFrame(all_results)
    df.to_csv(csv_path, index=False)
    with open(json_path, "w") as f:
        json.dump(all_api_payloads, f, indent=2)

    label = "TEST"
    print(f"\n[{label}] {len(all_results)} messages processed.")
    print(f"  CSV:   {csv_path}")
    print(f"  JSON:  {json_path}")


if __name__ == "__main__":
    run()

