# qradar-microsoft-network-hierarchy-importer

> Automatically download Microsoft Azure IP ranges and import them into IBM QRadar's Network Hierarchy — grouped by service tag, prefixed with `MS_`, and ready to use in rules and building blocks.

---

## Overview

Managing Microsoft IP ranges in QRadar is painful. Microsoft publishes thousands of CIDRs across hundreds of service tags (AzureCloud, Storage, AzureActiveDirectory, etc.) and updates them weekly. Adding them manually is not an option.

This project automates the full pipeline:

1. **Downloads** the latest Microsoft Service Tags JSON directly from Microsoft
2. **Parses** and deduplicates CIDRs across service tags
3. **Groups** them by root service (AzureCloud, Storage, AzureMonitor…)
4. **Uploads** them to QRadar's Network Hierarchy via REST API

Each service becomes a single collapsible row in the QRadar UI prefixed with `MS_`, so you can always tell what was added by this tool vs your own networks.

```
MS_AzureCloud              →  ~10,000 CIDRs
MS_AzureMonitor            →  ~1,700 CIDRs
MS_Storage                 →  ~1,800 CIDRs
MS_MicrosoftCloudAppSecurity → ~2,300 CIDRs
... (98 groups total)
```

Once imported, you can use these groups directly in QRadar rules:

```
AND NOT when the source IP is part of network MS_AzureCloud
AND NOT when the destination IP is part of network MS_AzureActiveDirectory
```

---

## Scripts

| Script | Purpose |
|---|---|
| `qradar_microsoft_import.py` | Main script — download & import |
| `stepback.py` | One-time rollback — remove entries created by this tool |

---

## Requirements

```bash
pip install requests
```

No other dependencies. The Microsoft JSON is downloaded automatically — no manual file handling needed.

---

## Configuration

Edit the constants at the top of each script:

```python
QRADAR_HOST = "https://<YOUR_QRADAR_IP>"   # QRadar console IP or hostname
API_TOKEN   = "<YOUR_API_TOKEN>"            # Admin → Authorized Services → Add
SKIP_IPV6   = True                          # Recommended: skip IPv6 ranges
DRY_RUN     = False                         # True = parse only, no upload
```

### Getting your API token

1. In QRadar go to **Admin → Authorized Services**
2. Click **Add Authorized Service**
3. Give it a name (e.g. `network-hierarchy-importer`) and **Admin** role
4. Copy the generated token

---

## Usage

### First run / weekly update

```bash
python3 qradar_microsoft_import.py
```

Then go to **QRadar → Admin → Deploy Changes** to activate the new networks in your rules.

```
============================================================
  Microsoft IP Ranges  →  QRadar Network Hierarchy Importer
============================================================

[1/5] 🌐 Downloading Microsoft IP ranges...
      Downloading: https://download.microsoft.com/.../ServiceTags_Public_20250422.json
      Change number : 20250422  |  Service tags: 847
      ✅ Downloaded in 4.2s

[2/5] 🔍 Parsing service tags into groups...
      ⚠  2602 CIDRs shared across groups → assigned to primary group
      ✅ Parsed in 1.1s
      📊 98 groups  |  42,138 unique IPv4 CIDRs

      GROUP                                            RANGES
      -------------------------------------------------------
      MS_ActionGroup                                      184
      MS_AzureCloud                                    10,475
      MS_AzureMonitor                                   1,766
      MS_Storage                                        1,838
      ...

[3/5] 📡 Reading current QRadar Network Hierarchy...
[4/5] 🔧 Building payload...
[5/5] 🚀 Uploading to QRadar...
      Payload size  : 8.3 MB
      Response in 12.4s  →  HTTP 200

============================================================
  ✅  SUCCESS
  New Microsoft networks       : 42,138
  Groups created               : 98
  Existing networks preserved  : 12
  Total time                   : 18.1s
============================================================

  ⚠️  ACTION REQUIRED:
  Go to QRadar → Admin → Deploy Changes
```

### Dry run (preview only)

```bash
python3 qradar_microsoft_import.py --dry-run
```

### Override download URL (if auto-detection fails)

```bash
python3 qradar_microsoft_import.py --url https://download.microsoft.com/.../ServiceTags_Public_YYYYMMDD.json
```

---

## Rollback

If you need to remove everything this tool imported and go back to your original Network Hierarchy:

```bash
# Preview what will be removed
python3 qradar_cleanup_old_groups.py --dry-run

# Execute
python3 qradar_cleanup_old_groups.py
```

Then **Admin → Deploy Changes**.

The cleanup script identifies entries created by this tool using the `description` field (`"Microsoft <ServiceName>"`). Your own networks are never touched, even if they share a group name like `Storage` or `Sql`.

---

## How it works

### Why one entry per CIDR?

The QRadar API stores one CIDR per network object (`"cidr"` is a single string). However, entries that share the same `name` are collapsed into a single row in the UI — exactly like the multi-CIDR textarea you see when editing a network object.

This script sets `name = group name` for all CIDRs in a group, so:
- **API level**: 42,138 entries (one per CIDR, required by QRadar)
- **UI level**: 98 rows (one per service group, collapsible)

### Why the `MS_` prefix?

Every group and entry name is prefixed with `MS_` so you can always distinguish Microsoft-managed networks from your own in the Network Hierarchy UI, the rule builder, and building blocks.

### How are shared CIDRs handled?

Microsoft publishes many CIDRs that appear in multiple service tags (e.g. a CIDR in both `AzureActiveDirectory` and `AzureActiveDirectoryDomainServices`). QRadar rejects duplicate CIDRs in the same payload. This script assigns each shared CIDR to its alphabetically first group and ignores it in the rest.

### How are previous imports cleaned up?

Before uploading, the script removes any existing entry whose `group` or `name` starts with `MS_`. This makes the script fully idempotent — safe to run multiple times or on a schedule.

---

## Scheduling (optional)

Microsoft updates the Service Tags JSON weekly. You can automate the import with a cron job:

```bash
# Every Monday at 06:00
0 6 * * 1 /usr/bin/python3 /opt/qradar-network-hierarchy-importer/qradar_microsoft_import.py >> /var/log/qradar_ms_import.log 2>&1
```

> Note: after each run you still need to manually trigger **Deploy Changes** in QRadar for the new networks to take effect in rules.

---

## Known limitations

- **QRadar validates against both deployed and staged networks**: the script reads both endpoints and merges them before uploading to avoid `422` errors on duplicate CIDRs.
- **IPv6 is skipped by default**: set `SKIP_IPV6 = False` to include IPv6 ranges (not supported in QRadar Network Hierarchy rules).
- **Large payload**: ~8 MB PUT request with 42,000+ entries. QRadar handles it fine but the request may take 10–30 seconds depending on hardware.
- **Deploy Changes required**: the API writes to `staged_networks`. Changes are not active in rules until you deploy.

---

## License

MIT
