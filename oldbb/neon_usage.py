#!/usr/bin/env python3
"""Neon compute usage and cost of the oldbb runs (11-10), from Neon's own billing data.

Reads the project events the Neon backend wrote (NEON_USAGE_LOG, one JSON line per project created / kept / deleted),
then for those projects asks Neon for
  1. GET /consumption_history/v2/projects (hourly; the endpoint Neon says to use for billing; Launch plan and up):
     compute_unit_seconds (CU-seconds), extra_branches_month, public_network_transfer_bytes, per project;
  2. GET /projects/{id} (projects still present, i.e. kept with NEON_KEEP_PROJECTS=1): compute_time_seconds,
     active_time_seconds, cpu_used_sec (Neon: not for billing; a cross-check).
Cost = CU-hours x NEON_CU_HOUR_USD (default 0.106, Launch, 11-10) + extra branch-months x 1.50.
Hourly data lag behind real time; run this an hour or more after the runs (hourly data are kept 168 h).
--delete then deletes the kept projects. The API key comes from NEON_API_KEY_ORG and is never printed.
Usage: neon_usage.py <usage log> [<usage log> ...] [--out FILE.json] [--delete]"""
import argparse
import datetime as dt
import json
import os
import sys
import time

import requests

API = "https://console.neon.tech/api/v2/"


def request(method, endpoint, **kw):
    key = os.environ.get("NEON_API_KEY_ORG", "")
    if not key:
        sys.exit("export NEON_API_KEY_ORG first")
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    for attempt in range(6):
        r = requests.request(method, API + endpoint, headers=headers, timeout=60, **kw)
        if r.status_code not in (423, 429) or attempt == 5:
            break
        time.sleep(5 * (attempt + 1))
    r.raise_for_status()
    return r.json() if r.content else {}


def iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def entry_metrics(entry):
    if isinstance(entry.get("metrics"), list):
        return {m.get("metric_name"): m.get("value") or 0 for m in entry["metrics"]}
    return {k: v for k, v in entry.items() if isinstance(v, (int, float))}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--out")
    ap.add_argument("--delete", action="store_true")
    args = ap.parse_args()
    rate = float(os.environ.get("NEON_CU_HOUR_USD", "0.106"))
    projects = {}
    for path in args.logs:
        for line in open(path):
            if line.strip():
                ev = json.loads(line)
                p = projects.setdefault(ev["project_id"], {"name": ev.get("name", ""), "events": {}})
                p["events"][ev["event"]] = ev["time"]
                p["name"] = p["name"] or ev.get("name", "")
                p["compute_cu"] = ev.get("compute_cu")
    if not projects:
        sys.exit("no projects in the logs")
    times = [t for p in projects.values() for t in p["events"].values()]
    t0, t1 = min(times) - 3600, max(time.time(), max(times)) + 3600
    org = os.environ.get("NEON_ORG_ID", "")
    for pid, p in projects.items():
        try:
            d = request("GET", f"projects/{pid}")["project"]
            p["exists"] = True
            org = org or d.get("org_id", "")
            p["project_counters"] = {k: d.get(k) for k in ("compute_time_seconds", "active_time_seconds",
                                                            "cpu_used_sec", "written_data_bytes",
                                                            "data_transfer_bytes", "consumption_period_start")}
        except requests.HTTPError as e:
            p["exists"] = e.response is None or e.response.status_code != 404
    ids = list(projects)
    raw = []
    for i in range(0, len(ids), 10):
        params = {"from": iso(t0), "to": iso(t1), "granularity": "hourly", "limit": 100,
                  "project_ids": ",".join(ids[i:i + 10]),
                  "metrics": "compute_unit_seconds,extra_branches_month,public_network_transfer_bytes"}
        if org:
            params["org_id"] = org
        try:
            resp = request("GET", "consumption_history/v2/projects", params=params)
        except requests.HTTPError as e:
            print(f"consumption_history/v2: HTTP {e.response.status_code if e.response is not None else '?'}: "
                  f"{(e.response.text if e.response is not None else '')[:300]}")
            continue
        raw.append(resp)
        for proj in resp.get("projects", []):
            sums = projects.setdefault(proj.get("project_id"), {"name": "", "events": {}}).setdefault("billing", {})
            for period in proj.get("periods", []):
                for entry in period.get("consumption", []):
                    for k, v in entry_metrics(entry).items():
                        sums[k] = sums.get(k, 0) + v
    total_cuh = total_usd = 0.0
    print(f"{'project':<28} {'name':<44} {'CU-h':>8} {'USD':>8}  counters (compute_time_s / active_time_s)")
    for pid, p in projects.items():
        b = p.get("billing", {})
        cuh = b.get("compute_unit_seconds", 0) / 3600
        usd = cuh * rate + b.get("extra_branches_month", 0) * 1.50
        p["cu_hours"], p["usd"] = round(cuh, 3), round(usd, 2)
        total_cuh += cuh
        total_usd += usd
        c = p.get("project_counters") or {}
        print(f"{pid:<28} {p['name'][:44]:<44} {cuh:8.2f} {usd:8.2f}  "
              f"{c.get('compute_time_seconds')} / {c.get('active_time_seconds')}")
    print(f"total: {total_cuh:.2f} CU-h, about {total_usd:.2f} USD at {rate} USD per CU-h "
          f"({len(projects)} projects, window {iso(t0)} .. {iso(t1)})")
    if args.out:
        with open(args.out, "w") as f:
            json.dump({"rate_usd_per_cu_hour": rate, "projects": projects, "raw": raw,
                       "total_cu_hours": total_cuh, "total_usd": total_usd}, f, indent=2)
        print(f"written {args.out}")
    if args.delete:
        for pid, p in projects.items():
            if p.get("exists"):
                try:
                    request("DELETE", f"projects/{pid}")
                    print(f"deleted {pid}")
                except requests.HTTPError as e:
                    print(f"could not delete {pid}: {e}")


if __name__ == "__main__":
    main()
