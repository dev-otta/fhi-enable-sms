## FHI Enable – Lifestyle SMS Campaign Script

This script drives a lifestyle SMS campaign from DHIS2. It reads campaign configuration and DHIS2 metadata from the DHIS2 dataStore and can run in test or live mode.

### Requirements

- Python 3.9+ with `requests` and `pandas` installed.
- Network access to your DHIS2 instance.
- A DHIS2 user with permission to read tracked entity data and manage the `sms-campaigns` dataStore namespace.

### DHIS2 connection configuration

The script connects to DHIS2 using environment variables. All three must be set:

- **DHIS2_URL**: Base API URL, e.g. `https://your-server.org/dhis/api`
- **DHIS2_USERNAME**: DHIS2 username
- **DHIS2_PASSWORD**: DHIS2 password

Example:

```bash
export DHIS2_URL="https://your-server.org/dhis/api"
export DHIS2_USERNAME="myuser"
export DHIS2_PASSWORD="mypassword"
python sms_script.py --next-week
```

### DataStore configuration (`sms-campaigns/config`)

The script expects a JSON object stored at:

- **Namespace**: `sms-campaigns`
- **Key**: `config`

The JSON combines scheduling, message content, and DHIS2 metadata:

```json
{
  "anchorMonday": "2026-01-05",
  "dhis2": {
    "programUid": "WSGAb5XwJ3Y",
    "profileStageUid": "iF5roNU7QWm",
    "birthOutcomeStageUid": "gEyexNoAqHB",
    "birthOutcomeDe": "VIEg1M2z5Vs",
    "phoneAttributeUid": "RJxLa3nITB3"
  },
  "campaigns": {
    "DIET": {
      "dayOfWeek": 0,
      "messages": ["Diet SMS 1", "Diet SMS 2"]
    },
    "PHYSICAL": {
      "dayOfWeek": 2,
      "messages": ["Physical SMS 1"]
    },
    "AIR": {
      "dayOfWeek": 4,
      "messages": ["Air SMS 1"]
    }
  }
}
```

- **anchorMonday**: A Monday ISO date that defines the start of the bi‑weekly cycle.
- **dhis2**: DHIS2 metadata for the program and attributes used to select and contact women.
  - **programUid**: Program UID.
  - **profileStageUid**: Program stage UID for the “Women’s profile and history” event.
  - **birthOutcomeStageUid**: Program stage UID that holds the birth/pregnancy outcome. The birth-outcome data element is only checked within this stage.
  - **birthOutcomeDe**: Data element UID for the Birth Outcome. When any non-empty value is recorded for this data element, the woman stops receiving messages.
  - **phoneAttributeUid**: Tracked entity attribute UID for the phone number.
- **campaigns.\*.dayOfWeek**: 0=Monday, 1=Tuesday, …, 6=Sunday.
- **campaigns.\*.messages**: Ordered list of SMS texts for that campaign.

To adapt the script to a different DHIS2 instance, update only the `dhis2` block and campaigns in the dataStore JSON; the code does not need changes.

### When messages stop

A woman who has a “Women’s profile and history” event receives campaign messages until **either** of the following stop conditions is reached, whichever comes first:

- **40 weeks after enrollment** — once the send date is on or after `enrollmentDate + 40 weeks`.
- **A birth outcome is recorded** — once the `birthOutcomeDe` data element holds any non-empty value in the `birthOutcomeStageUid` stage.

The 40-week window is fixed in the script (`WEEKS_UNTIL_STOP`).

### Creating or updating the DataStore entry

You can use the DHIS2 Datastore Manager app or plain HTTP requests.

Using HTTP (POST first time, PUT for updates):

```bash
curl -X POST -u "$DHIS2_USERNAME:$DHIS2_PASSWORD" \
  -H "Content-Type: application/json" \
  -d @sms_datastore_config.json \
  "$DHIS2_URL/dataStore/sms-campaigns/config"

curl -X PUT -u "$DHIS2_USERNAME:$DHIS2_PASSWORD" \
  -H "Content-Type: application/json" \
  -d @sms_datastore_config.json \
  "$DHIS2_URL/dataStore/sms-campaigns/config"
```

### Running the script

From the `sms` directory:

```bash
python sms_script.py
```

Key options:

- `--live`: Actually send SMS via the DHIS2 gateway. Without this flag, the script only exports messages.
- `--next-week`: Preview messages for next week’s Monday, Wednesday, and Friday.
- `--target-date YYYY-MM-DD`: Run for a specific date.

On completion the script writes or logs:

- In **test mode**:
  - `sms_test_YYYYMMDD_HHMMSS.csv`: one row per eligible TEI and campaign **with a phone number**, including status.
  - `sms_test_YYYYMMDD_HHMMSS.json`: array of outbound SMS payloads, each of the form `{"message": "<text>", "recipients": ["<phone>"]}`.
- In **live mode**:
  - No CSV/JSON is created for the run.
  - Each successful POST to the DHIS2 SMS API is appended as a JSON line to `sms_live.log` together with TEI, phone, campaign, date, and message.

### Cron example

Run every Monday, Wednesday, and Friday at 09:00 on a Unix-like system:

```bash
0 9 * * 1,3,5  cd /path/to/sms && DHIS2_URL="https://your-server.org/dhis/api" DHIS2_USERNAME="user" DHIS2_PASSWORD="pass" python3 sms_script.py --live >> sms_cron.log 2>&1
```

