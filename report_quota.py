#!/usr/bin/env python3
"""Report this machine's Muse subscription quota to the bridge (per API key).

Run this wherever a Muse account is logged in. The API key identifies WHICH
account the quota belongs to (1 account = 1 key). The bridge dashboard then
shows each account's real subscription quota, and blocks the key with 429
when that account's weekly limit is exhausted.

Usage:
  API_KEY=muse_sk_... python3 report_quota.py
  API_KEY=muse_sk_... BRIDGE_URL=https://<domain-kamu> python3 report_quota.py

For automatic updates every 30 minutes (cron):
  */30 * * * * API_KEY=muse_sk_... /usr/bin/python3 /path/to/report_quota.py

Requires the `subscription-status` CLI on this machine (comes with Muse).
"""
import json, os, re, subprocess, sys, urllib.request
from datetime import datetime, timezone, timedelta

WIB = timezone(timedelta(hours=7))
MONTHS = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
          "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}

BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8765").rstrip("/")
API_KEY = os.environ.get("API_KEY", "").strip()


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def main():
    if not API_KEY:
        print("API_KEY env var is required (the API key of this Muse account)")
        return 2
    r = run(["subscription-status", "status"])
    if r.returncode != 0:
        print("subscription-status failed:", r.stderr.strip()[:200])
        return 1
    out = r.stdout

    plan = "Free plan"
    m = re.search(r"(?im)^plan:\s*(.+)$", out)
    if m:
        plan = m.group(1).strip()
    has_subscription = "does not have a subscription" not in out

    used_pct = None
    m = re.search(r"Usage:\s*(\d+)%\s+of\s+free\s+weekly\s+limit", out)
    if m:
        used_pct = int(m.group(1))

    resets_at = None
    m = re.search(r"Weekly limit resets (\w{3}) (\d{1,2}) at (\d{1,2}):(\d{2})\s*(AM|PM)", out)
    if m:
        mon, day, hh, mm, ap = m.groups()
        hh = int(hh) % 12 + (12 if ap == "PM" else 0)
        now = datetime.now(WIB)
        year = now.year
        dt = datetime(year, MONTHS[mon], int(day), hh, int(mm), tzinfo=WIB)
        if dt <= now:
            dt = datetime(year + 1, MONTHS[mon], int(day), hh, int(mm), tzinfo=WIB)
        resets_at = dt.isoformat()

    add_used_pct, add_remaining = 0, None
    m = re.search(r"Additional tokens:\s*(\d+)%\s+used\s*\(([\d.]+)B tokens left\)", out)
    if m:
        add_used_pct = int(m.group(1))
        add_remaining = int(float(m.group(2)) * 1_000_000_000)

    data = {
        "plan": plan,
        "has_subscription": has_subscription,
        "weekly_limit": {"used_pct": used_pct, "resets_at": resets_at},
        "additional_tokens": {"used_pct": add_used_pct, "remaining": add_remaining},
        "updated_at": datetime.now(WIB).isoformat(),
    }

    req = urllib.request.Request(
        BRIDGE_URL + "/v1/real-quota",
        data=json.dumps(data).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print("reported:", resp.status, json.dumps(data)[:120])
            return 0
    except Exception as e:
        print("report failed:", str(e)[:200])
        return 1


if __name__ == "__main__":
    sys.exit(main())
