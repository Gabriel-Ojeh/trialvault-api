#!/usr/bin/env python3
"""
CliniScope Daily Registry Sync Script
======================================
Runs once per day (via cron or cloud scheduler) to download and cache
trial data from registries that don't have live browser-accessible APIs:
  - WHO ICTRP (aggregates 17 registries including PACTR, ANZCTR, CTRI, JRCT)
  - ISRCTN    (backup/bulk download)
  - EU CTIS   (full dataset download)

Results are saved as JSON files that your CliniScope backend (or even a
static JSON CDN) can serve to the frontend app.

SETUP:
  pip install requests pandas lxml python-dotenv schedule

DEPLOY OPTIONS:
  1. Cron on VPS:      0 2 * * * /usr/bin/python3 /app/cliniscope_daily_sync.py
  2. GitHub Actions:   Schedule workflow (see workflow template below)
  3. Railway/Render:   Set CRON_SCHEDULE env var and deploy
  4. Supabase Edge:    Use pg_cron extension to trigger a function daily

ENVIRONMENT VARIABLES (.env file or hosting dashboard):
  OUTPUT_DIR=/app/data           # Where to save JSON files
  NOTIFY_EMAIL=you@example.com   # Optional - email on completion/failure
  SMTP_HOST=smtp.gmail.com       # Optional SMTP for notifications
"""

import os
import json
import requests
import pandas as pd
import logging
import schedule
import time
from datetime import datetime, timezone
from io import StringIO, BytesIO
from pathlib import Path
from lxml import etree

# ── CONFIG ──────────────────────────────────────────────────────────────────
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "./cliniscope_data"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = OUTPUT_DIR / "sync.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("cliniscope_sync")

HEADERS = {
    "User-Agent": "CliniScope/1.0 (clinical trial aggregator; contact@cliniscope.io)"
}

# ── REGISTRY SOURCES ─────────────────────────────────────────────────────────

WHO_ICTRP_URL = (
    "https://trialsearch.who.int/api/search/json"  # WHO live search API
    # Alternatively, use bulk CSV export:
    # "https://trialsearch.who.int/TrialSearch.aspx" + form submit for CSV
)

# WHO ICTRP covers: ANZCTR, PACTR, CTRI (India), JRCT (Japan), DRKS (Germany),
#                   IRCT (Iran), REBEC (Brazil), REPEC (Peru), ChiCTR (China),
#                   ReBec, LBCTR, CRiS (Korea), and more

ISRCTN_API = "https://www.isrctn.com/api/query/format/who"

EU_CTIS_API = "https://euclinicaltrials.eu/ctis-public/rest/retrieve"


# ── WHO ICTRP SYNC ────────────────────────────────────────────────────────────

def sync_who_ictrp(conditions=None):
    """
    Fetches trials from WHO ICTRP search API.
    WHO ICTRP aggregates 17+ registries including PACTR, ANZCTR, CTRI India,
    JRCT Japan, DRKS Germany, ChiCTR China, and more.
    """
    if conditions is None:
        conditions = ["cancer", "diabetes", "HIV", "malaria", "tuberculosis",
                      "hypertension", "sickle cell", "COVID-19", "Alzheimer",
                      "kidney disease", "stroke", "obesity", "depression"]

    all_trials = []
    log.info(f"Starting WHO ICTRP sync for {len(conditions)} conditions...")

    for cond in conditions:
        try:
            # WHO ICTRP search endpoint
            params = {
                "query": cond,
                "rss": "1",
                "page_limit": 200,
                "date_type": "updated",
            }
            resp = requests.get(
                "https://trialsearch.who.int/search.aspx",
                params=params, headers=HEADERS, timeout=30
            )

            if resp.status_code != 200:
                log.warning(f"WHO ICTRP non-200 for '{cond}': {resp.status_code}")
                continue

            # Parse XML response (RSS/WHO ICTRP format)
            root = etree.fromstring(resp.content)
            ns = {"who": "http://trialsearch.who.int/ns/1.0"}

            # Try both formats
            trials_xml = root.findall(".//trial") or root.findall(".//item")

            for trial in trials_xml:
                def g(tag):
                    el = trial.find(tag)
                    return el.text.strip() if el is not None and el.text else ""

                all_trials.append({
                    "id": g("TrialID") or g("guid"),
                    "title": g("Public_title") or g("Scientific_title") or g("title"),
                    "status": g("Recruitment_status") or g("recruitment_status"),
                    "phase": g("Phase"),
                    "conditions": [g("Condition")],
                    "min_age": g("Agemin"),
                    "max_age": g("Agemax"),
                    "gender": g("Gender"),
                    "sponsor": g("Primary_sponsor"),
                    "countries": [c.strip() for c in g("Countries").split(";") if c.strip()],
                    "date_registered": g("Date_registration"),
                    "registry": g("Source_Register") or "WHO ICTRP",
                    "url": g("url") or g("link"),
                    "_source": "WHO_ICTRP",
                    "_synced_at": datetime.now(timezone.utc).isoformat(),
                })

            log.info(f"  WHO ICTRP '{cond}': {len(trials_xml)} trials")

        except Exception as e:
            log.error(f"  WHO ICTRP error for '{cond}': {e}")

    # Deduplicate by trial ID
    seen = set()
    unique = []
    for t in all_trials:
        if t["id"] and t["id"] not in seen:
            seen.add(t["id"])
            unique.append(t)

    # Save to JSON
    out_path = OUTPUT_DIR / "who_ictrp.json"
    with open(out_path, "w") as f:
        json.dump({
            "updated": datetime.now(timezone.utc).isoformat(),
            "count": len(unique),
            "trials": unique
        }, f, indent=2)

    log.info(f"✅ WHO ICTRP sync complete: {len(unique)} unique trials → {out_path}")
    return unique


# ── ISRCTN SYNC ────────────────────────────────────────────────────────────────

def sync_isrctn(conditions=None):
    """
    Syncs ISRCTN trials via their public WHO XML API.
    Covers UK-registered and many international trials.
    """
    if conditions is None:
        conditions = ["cancer", "diabetes", "HIV", "malaria", "hypertension",
                      "cardiovascular", "mental health", "pregnancy", "paediatric"]

    all_trials = []
    log.info(f"Starting ISRCTN sync for {len(conditions)} conditions...")

    for cond in conditions:
        try:
            resp = requests.get(
                ISRCTN_API,
                params={"q": cond, "limit": 200, "offset": 0},
                headers=HEADERS, timeout=30
            )

            if resp.status_code != 200:
                log.warning(f"ISRCTN non-200 for '{cond}': {resp.status_code}")
                continue

            root = etree.fromstring(resp.content)
            for trial in root.findall(".//trial"):
                def g(tag):
                    el = trial.find(tag)
                    return el.text.strip() if el is not None and el.text else ""

                all_trials.append({
                    "id": g("TrialID"),
                    "title": g("Public_title") or g("Scientific_title"),
                    "status": g("Recruitment_status"),
                    "phase": g("Phase"),
                    "conditions": [g("Condition")],
                    "min_age": g("Agemin"),
                    "max_age": g("Agemax"),
                    "gender": g("Gender"),
                    "sponsor": g("Primary_sponsor"),
                    "countries": [g("Countries")],
                    "date_registered": g("Date_registration"),
                    "registry": "ISRCTN",
                    "url": f"https://www.isrctn.com/{g('TrialID')}",
                    "_source": "ISRCTN",
                    "_synced_at": datetime.now(timezone.utc).isoformat(),
                })

            log.info(f"  ISRCTN '{cond}': {len(root.findall('.//trial'))} trials")

        except Exception as e:
            log.error(f"  ISRCTN error for '{cond}': {e}")

    # Deduplicate
    seen = set()
    unique = [t for t in all_trials if t["id"] not in seen and not seen.add(t["id"])]

    out_path = OUTPUT_DIR / "isrctn.json"
    with open(out_path, "w") as f:
        json.dump({
            "updated": datetime.now(timezone.utc).isoformat(),
            "count": len(unique),
            "trials": unique
        }, f, indent=2)

    log.info(f"✅ ISRCTN sync complete: {len(unique)} unique trials → {out_path}")
    return unique


# ── EU CTIS SYNC ────────────────────────────────────────────────────────────────

def sync_euctr(pages=10):
    """
    Syncs EU CTIS trials via their REST API.
    Covers all EU member state clinical trials registered since 2022.
    """
    all_trials = []
    log.info(f"Starting EU CTIS sync ({pages} pages × 100 results)...")

    for page in range(pages):
        try:
            body = {
                "pagination": {"page": page, "size": 100},
                "sort": {"property": "dateOfCreation", "isAscending": False},
                "searchCriteria": {}  # Empty = all trials
            }
            resp = requests.post(
                EU_CTIS_API,
                json=body,
                headers={**HEADERS, "Content-Type": "application/json"},
                timeout=30
            )

            if resp.status_code != 200:
                log.warning(f"EU CTIS non-200 on page {page}: {resp.status_code}")
                break

            data = resp.json()
            items = data.get("data", data.get("results", data.get("content", [])))

            if not items:
                log.info(f"  EU CTIS page {page}: no more results")
                break

            for t in items:
                all_trials.append({
                    "id": t.get("ctNumber") or t.get("eudraCtNumber", ""),
                    "title": t.get("ctTitle") or t.get("title", "Untitled"),
                    "status": t.get("trialStatus") or t.get("status", ""),
                    "phase": t.get("trialPhase") or t.get("phase", ""),
                    "conditions": t.get("conditions", [t.get("medicalCondition", "")]),
                    "min_age": t.get("ageMin", ""),
                    "max_age": t.get("ageMax", ""),
                    "gender": t.get("gender", ""),
                    "sponsor": t.get("sponsorName") or t.get("sponsor", ""),
                    "countries": t.get("memberStates") or t.get("countries", []),
                    "date_registered": t.get("dateOfCreation", ""),
                    "registry": "EU CTIS",
                    "url": f"https://euclinicaltrials.eu/search-for-clinical-trials/?lang=en&eudraCTNumber={t.get('ctNumber','')}",
                    "_source": "EUCTR",
                    "_synced_at": datetime.now(timezone.utc).isoformat(),
                })

            log.info(f"  EU CTIS page {page}: {len(items)} trials")

        except Exception as e:
            log.error(f"  EU CTIS error on page {page}: {e}")
            break

    out_path = OUTPUT_DIR / "euctr.json"
    with open(out_path, "w") as f:
        json.dump({
            "updated": datetime.now(timezone.utc).isoformat(),
            "count": len(all_trials),
            "trials": all_trials
        }, f, indent=2)

    log.info(f"✅ EU CTIS sync complete: {len(all_trials)} trials → {out_path}")
    return all_trials


# ── MERGE ALL INTO UNIFIED INDEX ─────────────────────────────────────────────

def merge_all_registries():
    """
    Merges all synced JSON files into a single searchable index.
    This is what your CliniScope API endpoint would serve.
    """
    merged = []
    sources = ["who_ictrp.json", "isrctn.json", "euctr.json"]

    for src in sources:
        path = OUTPUT_DIR / src
        if path.exists():
            with open(path) as f:
                data = json.load(f)
                merged.extend(data.get("trials", []))
            log.info(f"  Merged {src}: {len(data.get('trials',[]))} trials")

    out_path = OUTPUT_DIR / "all_registries.json"
    with open(out_path, "w") as f:
        json.dump({
            "updated": datetime.now(timezone.utc).isoformat(),
            "count": len(merged),
            "sources": sources,
            "trials": merged
        }, f, indent=2)

    log.info(f"✅ Unified index: {len(merged)} total trials → {out_path}")


# ── MAIN SYNC JOB ─────────────────────────────────────────────────────────────

def run_full_sync():
    log.info("=" * 60)
    log.info(f"CliniScope daily sync started at {datetime.now(timezone.utc).isoformat()}")
    log.info("=" * 60)

    try:
        sync_who_ictrp()
    except Exception as e:
        log.error(f"WHO ICTRP sync failed: {e}")

    try:
        sync_isrctn()
    except Exception as e:
        log.error(f"ISRCTN sync failed: {e}")

    try:
        sync_euctr()
    except Exception as e:
        log.error(f"EU CTIS sync failed: {e}")

    merge_all_registries()

    log.info("=" * 60)
    log.info("Daily sync complete.")
    log.info("=" * 60)


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if "--now" in sys.argv or "--run" in sys.argv:
        # Run immediately (for testing or first-time setup)
        run_full_sync()
    else:
        # Schedule to run daily at 02:00 UTC
        log.info("CliniScope sync scheduler started. Next run at 02:00 UTC daily.")
        schedule.every().day.at("02:00").do(run_full_sync)

        # Also run once immediately on startup
        run_full_sync()

        while True:
            schedule.run_pending()
            time.sleep(60)


# ─────────────────────────────────────────────────────────────────────────────
# DEPLOYMENT GUIDE
# ─────────────────────────────────────────────────────────────────────────────
#
# OPTION 1: VPS (DigitalOcean $6/mo, Hetzner $4/mo, Linode $5/mo)
# ────────────────────────────────────────────────────────────────
#   pip install -r requirements.txt
#   crontab -e
#   # Add: 0 2 * * * cd /app && python3 cliniscope_daily_sync.py --now
#
# OPTION 2: GitHub Actions (FREE)
# ────────────────────────────────
#   # .github/workflows/daily_sync.yml
#   name: Daily Registry Sync
#   on:
#     schedule:
#       - cron: '0 2 * * *'   # 02:00 UTC every day
#     workflow_dispatch:        # Manual trigger
#   jobs:
#     sync:
#       runs-on: ubuntu-latest
#       steps:
#         - uses: actions/checkout@v4
#         - uses: actions/setup-python@v5
#           with: { python-version: '3.11' }
#         - run: pip install requests pandas lxml schedule
#         - run: python cliniscope_daily_sync.py --now
#         - uses: actions/upload-artifact@v4
#           with:
#             name: registry-data
#             path: cliniscope_data/
#
# OPTION 3: Railway / Render / Fly.io (FREE tier available)
# ─────────────────────────────────────────────────────────
#   # Set environment variable: RUN_MODE=scheduler
#   # Deploy as a background worker (no web port needed)
#   # Railway will keep it running and restart on crash
#
# OPTION 4: Supabase (FREE tier, stores results in PostgreSQL)
# ────────────────────────────────────────────────────────────
#   # Use pg_cron to schedule, store results in a `trials` table
#   # CliniScope frontend queries: GET /rest/v1/trials?source=eq.ISRCTN
#
# ─────────────────────────────────────────────────────────────────────────────
# SERVING THE DATA TO CLINISCOPE FRONTEND
# ─────────────────────────────────────────────────────────────────────────────
#
# Once synced, the JSON files need to be served via an API.
# Simplest options:
#
#   A) GitHub Pages / Netlify (static JSON, free):
#      Commit output JSONs to a repo → available at:
#      https://yourname.github.io/cliniscope-data/isrctn.json
#
#   B) FastAPI micro-server (add search/filter endpoint):
#      from fastapi import FastAPI
#      import json
#      app = FastAPI()
#      data = json.load(open('cliniscope_data/all_registries.json'))
#
#      @app.get("/search")
#      def search(q: str, source: str = None):
#          trials = data['trials']
#          if q:
#              trials = [t for t in trials if q.lower() in (t.get('title','') + ' '.join(t.get('conditions',[]))).lower()]
#          if source:
#              trials = [t for t in trials if t.get('_source') == source]
#          return {"count": len(trials), "trials": trials[:100]}
#
#   C) Supabase (free PostgreSQL + auto REST API):
#      Load JSON into Supabase table, get instant REST API.
#
# ─────────────────────────────────────────────────────────────────────────────
