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

AUTH = (DHIS2_USERNAME, DHIS2_PASSWORD)
HEADERS = {"Content-Type": "application/json"}

# ── DHIS2 UIDs ────────────────────────────────────────────────────────
PROGRAM_UID = None
PROFILE_STAGE_UID = None
ANC_EXAM_STAGE_UID = None
ANC_VISIT_NUMBER_DE = None
PHONE_ATTRIBUTE_UID = None

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
    resp = requests.get(url, auth=AUTH)

    if resp.status_code == 404:
        print(
            f"  ✗ DataStore key '{DATASTORE_NAMESPACE}/{DATASTORE_KEY}' not found.\n"
            f"    Upload sms_datastore_config.json first — see bottom of this script."
        )
        sys.exit(1)

    resp.raise_for_status()
    config = resp.json()

    dhis2_cfg = config.get("dhis2", {})
    global PROGRAM_UID, PROFILE_STAGE_UID, ANC_EXAM_STAGE_UID, ANC_VISIT_NUMBER_DE, PHONE_ATTRIBUTE_UID
    PROGRAM_UID = dhis2_cfg.get("programUid", PROGRAM_UID)
    PROFILE_STAGE_UID = dhis2_cfg.get("profileStageUid", PROFILE_STAGE_UID)
    ANC_EXAM_STAGE_UID = dhis2_cfg.get("ancExamStageUid", ANC_EXAM_STAGE_UID)
    ANC_VISIT_NUMBER_DE = dhis2_cfg.get("ancVisitNumberDe", ANC_VISIT_NUMBER_DE)
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
            "enrollments[events[programStage,status,dataValues[dataElement,value]]]"
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


def is_eligible(tei):
    """
    Eligible when the woman:
      • HAS  a "Women's profile and history" event
      • Does NOT have a COMPLETED ANC Examination event where
        ANC Visit Number = 8  (a SCHEDULE'd visit 8 is still eligible)
    """
    all_events = [
        ev
        for enroll in tei.get("enrollments", [])
        for ev in enroll.get("events", [])
    ]

    has_profile = any(
        ev["programStage"] == PROFILE_STAGE_UID for ev in all_events
    )
    if not has_profile:
        return False

    for ev in all_events:
        if ev["programStage"] != ANC_EXAM_STAGE_UID:
            continue
        if ev.get("status") != "COMPLETED":
            continue
        for dv in ev.get("dataValues", []):
            if (
                dv["dataElement"] == ANC_VISIT_NUMBER_DE
                and str(dv["value"]) == "8"
            ):
                return False
    return True


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
            if not is_eligible(tei):
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
    for td in target_dates:
        active = {n: c for n, c in campaigns.items() if is_send_day(c, anchor, td)}
        tag = ", ".join(active.keys()) if active else "—"
        print(f"  {td.strftime('%A %Y-%m-%d'):>20s} → {tag}")
        rows, api_payloads = process_date(td, anchor, campaigns, teis, is_live)
        all_results.extend(rows)
        all_api_payloads.extend(api_payloads)

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

