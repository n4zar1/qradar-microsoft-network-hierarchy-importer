#!/usr/bin/env python3
"""
Microsoft IP Ranges → QRadar Network Hierarchy
===============================================
Downloads the latest Azure/Microsoft IP ranges directly from Microsoft
and imports them into QRadar's Network Hierarchy via REST API.

Each root service tag (AzureCloud, Storage, AzureMonitor, etc.) becomes
a separate group prefixed with "MS_" so you can always tell what was
added by this script vs your own networks.

Usage:
    python3 qradar_microsoft_import.py
    python3 qradar_microsoft_import.py --dry-run
    python3 qradar_microsoft_import.py --url https://download.microsoft.com/.../ServiceTags_Public_YYYYMMDD.json

Requirements:
    pip install requests
"""

import re
import sys
import gzip
import json
import time
import argparse
import urllib3
import requests
from collections import defaultdict
from html.parser import HTMLParser
from urllib.request import urlopen, Request

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ══════════════════════════════════════════════════════════════════
#  CONFIGURATION  —  Edit these values
# ══════════════════════════════════════════════════════════════════
QRADAR_HOST = " "   # Ej: https://192.168.1.100
API_TOKEN   = " "
SKIP_IPV6   = True                          # Skip IPv6 ranges
DRY_RUN     = False                         # True = parse & show only, no upload
# ══════════════════════════════════════════════════════════════════

QRADAR_HEADERS = {
    "SEC": API_TOKEN,
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Version": "12.0"
}

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

MS_DOWNLOAD_PAGE = "https://www.microsoft.com/en-us/download/confirmation.aspx?id=56519"
MS_GROUP_PREFIX  = "MS_"   # Prefix added to every group name in QRadar


# ──────────────────────────────────────────────────────────────────
#  STEP 1 — Download Microsoft IP ranges
# ──────────────────────────────────────────────────────────────────

class _DownloadLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.download_url = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            if "ServiceTags_Public_" in href and href.endswith(".json"):
                self.download_url = href


def get_latest_download_url():
    """Scrape Microsoft's confirmation page to find the current JSON URL."""
    for page in [MS_DOWNLOAD_PAGE,
                 "https://www.microsoft.com/en-us/download/details.aspx?id=56519"]:
        print(f"   Fetching download page: {page}")
        try:
            req = Request(page, headers=BROWSER_HEADERS)
            with urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                html = raw.decode("utf-8", errors="replace")

            parser = _DownloadLinkParser()
            parser.feed(html)
            if parser.download_url:
                return parser.download_url

            # Fallback: regex
            match = re.search(
                r"https://download\.microsoft\.com/[^\s\"']+ServiceTags_Public_\d+\.json",
                html,
            )
            if match:
                return match.group(0)

        except Exception as exc:
            print(f"   Warning: {exc} — trying next URL...")

    raise RuntimeError(
        "Could not auto-detect the download URL.\n"
        "Pass it manually with --url:\n"
        "  python3 qradar_microsoft_import.py "
        "--url https://download.microsoft.com/.../ServiceTags_Public_YYYYMMDD.json"
    )


def download_microsoft_json(url):
    """Download and parse the Microsoft Service Tags JSON."""
    print(f"   Downloading: {url}")
    req = Request(url, headers=BROWSER_HEADERS)
    with urlopen(req, timeout=120) as resp:
        raw = resp.read()
        enc = resp.headers.get("Content-Encoding", "")
    if enc == "gzip" or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    data = json.loads(raw)
    print(f"   Change number : {data.get('changeNumber')}")
    print(f"   Cloud         : {data.get('cloud')}")
    print(f"   Service tags  : {len(data.get('values', []))}")
    return data


# ──────────────────────────────────────────────────────────────────
#  STEP 2 — Parse into groups
# ──────────────────────────────────────────────────────────────────

def build_groups(data):
    """
    Parse Microsoft JSON into a dict: { "AzureCloud": ["1.2.3.0/24", ...], ... }

    Rules:
    - Group = root service tag (text before the first dot).
      e.g.  AzureCloud.WestEurope  →  AzureCloud
    - IPv6 ranges are skipped if SKIP_IPV6 = True.
    - A CIDR that appears in multiple groups is assigned to the first
      one alphabetically. QRadar rejects duplicate CIDRs in the payload.
    """
    cidr_to_groups = defaultdict(list)

    for entry in data.get("values", []):
        service_name = entry.get("name", "")
        root_group   = service_name.split(".")[0]
        prefixes     = entry.get("properties", {}).get("addressPrefixes", [])

        for cidr in prefixes:
            if SKIP_IPV6 and ":" in cidr:
                continue
            if root_group not in cidr_to_groups[cidr]:
                cidr_to_groups[cidr].append(root_group)

    groups      = defaultdict(list)
    shared_count = 0

    for cidr, cidr_groups in cidr_to_groups.items():
        primary = sorted(cidr_groups)[0]
        groups[primary].append(cidr)
        if len(cidr_groups) > 1:
            shared_count += 1

    if shared_count:
        print(f"   ⚠  {shared_count} CIDRs shared across multiple groups "
              f"→ assigned to alphabetically first group")

    return {k: sorted(v) for k, v in sorted(groups.items())}


# ──────────────────────────────────────────────────────────────────
#  STEP 3 — Read QRadar current state
# ──────────────────────────────────────────────────────────────────

def get_existing_networks():
    """
    Read both /networks (deployed) and /staged_networks and merge by CIDR.
    QRadar validates PUT against both, so we must clean both.
    """
    combined = {}
    for endpoint in ["networks", "staged_networks"]:
        resp = requests.get(
            f"{QRADAR_HOST}/api/config/network_hierarchy/{endpoint}",
            headers=QRADAR_HEADERS,
            verify=False,
            timeout=30
        )
        if resp.status_code == 200:
            for entry in resp.json():
                combined[entry.get("cidr")] = entry

    result = list(combined.values())
    print(f"   {len(result)} unique entries read (deployed + staged merged)")
    return result


def remove_microsoft_entries(existing, new_cidrs):
    """
    Remove from the existing list:
    - Any entry whose CIDR is in the new batch (avoids 'already defined' 422).
    - Any entry whose group starts with MS_ (cleans up previous script runs).
    """
    new_cidr_set = set(new_cidrs)
    cleaned = [
        n for n in existing
        if n.get("cidr") not in new_cidr_set
        and not n.get("group", "").startswith(MS_GROUP_PREFIX)
        and not n.get("name", "").startswith(MS_GROUP_PREFIX)
    ]
    removed = len(existing) - len(cleaned)
    if removed:
        print(f"   🧹 {removed} previous Microsoft entries removed")
    return cleaned


# ──────────────────────────────────────────────────────────────────
#  STEP 4 — Build payload
# ──────────────────────────────────────────────────────────────────

def build_network_entries(groups, start_id):
    """
    Build the list of network objects for the QRadar API.

    The QRadar API stores one CIDR per entry (the 'cidr' field is a single
    string). However, entries that share the same 'name' are displayed
    collapsed under a single row in the UI — exactly like the multi-CIDR
    textarea you see when editing a network object.

    Strategy: every CIDR in a group gets the SAME name (the group name),
    so the entire group collapses into ONE visible row in the UI:
        MS_AzureCloud  →  1 row  (10,475 CIDRs underneath)
        MS_Storage     →  1 row  (1,838 CIDRs underneath)
        ...
    This keeps the Network Hierarchy readable without losing any coverage.
    """
    entries    = []
    current_id = start_id

    for group_name, cidrs in groups.items():
        shared_name = f"{MS_GROUP_PREFIX}{group_name}"   # e.g. MS_AzureCloud
        for cidr in cidrs:
            entries.append({
                "id":          current_id,
                "group":       shared_name,
                "name":        shared_name,              # same name → collapses in UI
                "cidr":        cidr,
                "description": f"Microsoft {group_name}",
                "domain_id":   0
            })
            current_id += 1

    return entries


# ──────────────────────────────────────────────────────────────────
#  STEP 5 — Upload to QRadar
# ──────────────────────────────────────────────────────────────────

def upload_networks(all_networks):
    """PUT /staged_networks — replaces the entire staged hierarchy."""
    payload_bytes = json.dumps(all_networks).encode()
    payload_mb    = len(payload_bytes) / 1024 / 1024
    print(f"   Payload size  : {payload_mb:.1f} MB")
    print(f"   Sending PUT request... (may take several seconds)")

    t0   = time.time()
    resp = requests.put(
        f"{QRADAR_HOST}/api/config/network_hierarchy/staged_networks",
        headers=QRADAR_HEADERS,
        verify=False,
        timeout=300,
        data=payload_bytes
    )
    elapsed = time.time() - t0
    print(f"   Response in {elapsed:.1f}s  →  HTTP {resp.status_code}")
    return resp


# ──────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────

def progress_bar(current, total, width=40):
    pct    = current / total
    filled = int(width * pct)
    bar    = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {current:>6}/{total} ({pct*100:.1f}%)"


def parse_args():
    p = argparse.ArgumentParser(
        description="Download Microsoft IP ranges and import into QRadar Network Hierarchy"
    )
    p.add_argument("--url",     help="Override the JSON download URL (skip auto-detection)")
    p.add_argument("--dry-run", action="store_true", help="Parse only, do not upload to QRadar")
    return p.parse_args()


# ──────────────────────────────────────────────────────────────────
#  Main
# ──────────────────────────────────────────────────────────────────

def main():
    args    = parse_args()
    dry_run = args.dry_run or DRY_RUN
    t_start = time.time()

    print("=" * 62)
    print("  Microsoft IP Ranges  →  QRadar Network Hierarchy Importer")
    print("=" * 62)

    # ── STEP 1: Download Microsoft data ─────────────────────────
    print("\n[1/5] 🌐 Downloading Microsoft IP ranges...")
    t = time.time()
    try:
        url  = args.url or get_latest_download_url()
        data = download_microsoft_json(url)
    except Exception as exc:
        print(f"\n   ❌ Download failed: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"   ✅ Downloaded in {time.time()-t:.1f}s")

    # ── STEP 2: Parse into groups ────────────────────────────────
    print("\n[2/5] 🔍 Parsing service tags into groups...")
    t      = time.time()
    groups = build_groups(data)
    total_cidrs = sum(len(v) for v in groups.values())
    print(f"   ✅ Parsed in {time.time()-t:.1f}s")
    print(f"   📊 {len(groups)} groups  |  {total_cidrs:,} unique IPv4 CIDRs\n")

    print(f"   {'GROUP':<47} {'RANGES':>7}")
    print(f"   {'-'*55}")
    for group_name, cidrs in groups.items():
        print(f"   {MS_GROUP_PREFIX}{group_name:<45} {len(cidrs):>7}")
    print(f"   {'-'*55}")
    print(f"   {'TOTAL':<47} {total_cidrs:>7}")

    if dry_run:
        print("\n⚠️  DRY RUN — nothing uploaded to QRadar.")
        print("   Set DRY_RUN = False (or remove --dry-run) to upload.")
        return

    # ── STEP 3: Read current QRadar state ───────────────────────
    print(f"\n[3/5] 📡 Reading current QRadar Network Hierarchy...")
    print(f"      Host: {QRADAR_HOST}")
    t        = time.time()
    existing = get_existing_networks()
    print(f"   ✅ Read in {time.time()-t:.1f}s")

    all_new_cidrs = [cidr for cidrs in groups.values() for cidr in cidrs]
    existing      = remove_microsoft_entries(existing, all_new_cidrs)
    print(f"   📋 Non-Microsoft networks preserved: {len(existing)}")

    # ── STEP 4: Build payload ────────────────────────────────────
    print(f"\n[4/5] 🔧 Building payload...")
    t          = time.time()
    max_id     = max((n.get("id", 0) for n in existing), default=1000)
    group_list = list(groups.items())
    new_entries = []
    current_id  = max_id + 1

    for i, (group_name, cidrs) in enumerate(group_list, 1):
        for cidr in cidrs:
            safe_name = cidr.replace("/", "_").replace(".", "_")
            new_entries.append({
                "id":          current_id,
                "group":       f"{MS_GROUP_PREFIX}{group_name}",
                "name":        safe_name,
                "cidr":        cidr,
                "description": f"Microsoft {group_name}",
                "domain_id":   0
            })
            current_id += 1
        print(f"   {progress_bar(i, len(group_list))}  {group_name:<45}", end="\r")

    print(f"   {progress_bar(len(group_list), len(group_list))}  {'Done':<45}")
    print(f"   ✅ {len(new_entries):,} entries built in {time.time()-t:.1f}s")

    # ── STEP 5: Upload ───────────────────────────────────────────
    all_networks = existing + new_entries
    print(f"\n[5/5] 🚀 Uploading to QRadar...")
    print(f"   Total entries in payload : {len(all_networks):,}  "
          f"({len(existing)} existing + {len(new_entries):,} new)")
    t    = time.time()
    resp = upload_networks(all_networks)

    # ── Result ───────────────────────────────────────────────────
    elapsed = time.time() - t_start
    print(f"\n{'='*62}")
    if resp.status_code == 200:
        result = resp.json()
        print("  ✅  SUCCESS")
        print(f"{'='*62}")
        print(f"  Entries in staged hierarchy  : {len(result):,}")
        print(f"  New Microsoft networks       : {len(new_entries):,}")
        print(f"  Groups created               : {len(groups)}")
        print(f"  Existing networks preserved  : {len(existing)}")
        print(f"  Total time                   : {elapsed:.1f}s")
        print(f"{'='*62}")
        print()
        print("  ⚠️  ACTION REQUIRED:")
        print("  Go to QRadar → Admin → Deploy Changes")
        print("  to activate the new networks in your rules.")
    else:
        print(f"  ❌  ERROR  (HTTP {resp.status_code})")
        print(f"{'='*62}")
        print(f"  Time elapsed : {elapsed:.1f}s")
        print(f"  Detail       : {resp.text[:600]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
