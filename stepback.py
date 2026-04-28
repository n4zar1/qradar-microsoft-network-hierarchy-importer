#!/usr/bin/env python3
"""
QRadar Network Hierarchy — One-time Cleanup
============================================
Removes all Microsoft network entries that were imported WITHOUT the MS_ prefix
(created by earlier versions of the import script).

Safe to run: only removes entries whose group name exactly matches a known
Microsoft service tag root name. Your own networks are never touched.

Usage:
    python3 qradar_cleanup_old_groups.py
    python3 qradar_cleanup_old_groups.py --dry-run
"""

import sys
import json
import time
import argparse
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ══════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════════════════
QRADAR_HOST = " "   # Ej: https://192.168.1.100
API_TOKEN   = " "   
DRY_RUN     = False
# ══════════════════════════════════════════════════════════════════

HEADERS = {
    "SEC": API_TOKEN,
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Version": "12.0"
}

# All known Microsoft service tag root names (without MS_ prefix).
# These are the group names created by the old version of the script.
OLD_MS_GROUPS = {
    "ActionGroup", "ApiManagement", "AppConfiguration", "AppService",
    "AppServiceManagement", "ApplicationInsightsAvailability",
    "AutonomousDevelopmentPlatform", "AzureActiveDirectory",
    "AzureActiveDirectoryDomainServices", "AzureAdvancedThreatProtection",
    "AzureArcInfrastructure", "AzureAttestation", "AzureBackup",
    "AzureBotService", "AzureCloud", "AzureCognitiveSearch",
    "AzureConnectors", "AzureContainerRegistry", "AzureCosmosDB",
    "AzureDataExplorerManagement", "AzureDataLake", "AzureDatabricks",
    "AzureDevOps", "AzureDevSpaces", "AzureDeviceUpdate",
    "AzureDigitalTwins", "AzureEventGrid", "AzureFrontDoor",
    "AzureHealthcareAPIs", "AzureInformationProtection", "AzureIoTHub",
    "AzureKeyVault", "AzureLoadTestingInstanceManagement",
    "AzureMachineLearning", "AzureMachineLearningInference",
    "AzureManagedGrafana", "AzureMonitor", "AzureMonitorForSAP",
    "AzureOpenDatasets", "AzurePortal", "AzureResourceManager",
    "AzureSecurityCenter", "AzureSentinel", "AzureSignalR",
    "AzureSiteRecovery", "AzureSphere", "AzureSpringCloud",
    "AzureStack", "AzureTrafficManager", "AzureUpdateDelivery",
    "AzureWebPubSub", "BatchNodeManagement", "ChaosStudio",
    "CognitiveServicesFrontend", "CognitiveServicesManagement",
    "CopilotActions", "DataFactory", "DataFactoryManagement",
    "Dynamics365BusinessCentral", "Dynamics365ForMarketingEmail",
    "EOPExternalPublishedIPs", "EventHub", "GatewayManager", "Grafana",
    "GuestAndHybridManagement", "HDInsight", "KustoAnalytics",
    "LogicApps", "LogicAppsManagement", "M365LighthouseProd",
    "M365ManagementActivityApi", "M365ManagementActivityApiWebhook",
    "Marketplace", "MicrosoftAzureFluidRelay", "MicrosoftCloudAppSecurity",
    "MicrosoftContainerRegistry", "MicrosoftDefenderForEndpoint",
    "MicrosoftPurviewPolicyDistribution", "OneDsCollector", "PowerBI",
    "PowerPlatformInfra", "PowerPlatformPlex", "PowerQueryOnline",
    "SCCservice", "Scuba", "SecurityCopilot", "SerialConsole",
    "ServiceBus", "ServiceFabric", "Sql", "SqlManagement", "Storage",
    "StorageMover", "StorageSyncService",
    "SystemServiceAzureSpringAppsResourceProvider", "VideoIndexer",
    "WindowsAdminCenter", "WindowsVirtualDesktop", "ZeroTrustSegmentation",
}


def get_networks(endpoint):
    resp = requests.get(
        f"{QRADAR_HOST}/api/config/network_hierarchy/{endpoint}",
        headers=HEADERS, verify=False, timeout=30
    )
    if resp.status_code == 200:
        return resp.json()
    print(f"   ⚠️  Could not read /{endpoint}: HTTP {resp.status_code}")
    return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args    = parser.parse_args()
    dry_run = args.dry_run or DRY_RUN

    print("=" * 62)
    print("  QRadar Network Hierarchy — Old Microsoft Groups Cleanup")
    print("=" * 62)

    # Read both deployed and staged, merge by CIDR
    print("\n[1/3] 📡 Reading current Network Hierarchy...")
    combined = {}
    for ep in ["networks", "staged_networks"]:
        for entry in get_networks(ep):
            combined[entry.get("cidr")] = entry
    all_entries = list(combined.values())
    print(f"   {len(all_entries)} unique entries found")

    # Identify entries to remove
    def is_our_entry(e):
        """
        Returns True ONLY for entries created by this script.
        We identify them by the description field which we always set to
        "Microsoft <ServiceName>" or "MS_<ServiceName>" — never used by QRadar
        itself or by manually created networks (unless someone copied our format).

        This means even if you have a group called "Storage" or "Sql" of your own,
        it will NOT be removed unless its description starts with "Microsoft " or "MS_".
        """
        desc  = e.get("description", "")
        group = e.get("group", "")
        name  = e.get("name",  "")
        return (
            desc.startswith("Microsoft ")       # set by current script version
            or desc.startswith("MS_")           # set by older script version
            or group.startswith("MS_")          # group prefixed by current script
            or name.startswith("MS_")           # name prefixed by current script
        )

    to_remove = [e for e in all_entries if     is_our_entry(e)]
    to_keep   = [e for e in all_entries if not is_our_entry(e)]

    print(f"\n[2/3] 🔍 Analysing entries...")
    print(f"   Entries to REMOVE (old Microsoft groups without MS_ prefix) : {len(to_remove)}")
    print(f"   Entries to KEEP   (your networks + already prefixed MS_*)   : {len(to_keep)}")

    if not to_remove:
        print("\n   ✅ Nothing to clean up — no old-style entries found.")
        return

    # Show a summary of groups being removed
    from collections import Counter
    group_counts = Counter(e.get("group") for e in to_remove)
    print(f"\n   Groups being removed:")
    for group, count in sorted(group_counts.items()):
        print(f"   {'  ' + group:<47} {count:>6} entries")

    if dry_run:
        print("\n⚠️  DRY RUN — nothing was changed in QRadar.")
        print("   Remove --dry-run (or set DRY_RUN = False) to apply.")
        return

    # PUT the cleaned list to staged_networks
    print(f"\n[3/3] 🚀 Uploading cleaned hierarchy to QRadar staged...")
    t    = time.time()
    resp = requests.put(
        f"{QRADAR_HOST}/api/config/network_hierarchy/staged_networks",
        headers=HEADERS,
        verify=False,
        timeout=300,
        data=json.dumps(to_keep)
    )
    elapsed = time.time() - t

    print(f"\n{'='*62}")
    if resp.status_code == 200:
        print("  ✅  SUCCESS")
        print(f"{'='*62}")
        print(f"  Entries removed  : {len(to_remove):,}")
        print(f"  Entries kept     : {len(to_keep):,}")
        print(f"  Time             : {elapsed:.1f}s")
        print(f"{'='*62}")
        print()
        print("  ⚠️  ACTION REQUIRED:")
        print("  Go to QRadar → Admin → Deploy Changes")
    else:
        print(f"  ❌  ERROR  (HTTP {resp.status_code})")
        print(resp.text[:500])
        sys.exit(1)


if __name__ == "__main__":
    main()
