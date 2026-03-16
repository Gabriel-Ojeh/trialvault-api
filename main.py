"""
TrialVault — FastAPI Backend
=============================
Handles: auth validation, search proxying, Supabase integration,
         Paystack subscriptions, alert scheduling, daily sync trigger.

INSTALL:
  pip install fastapi uvicorn supabase python-dotenv httpx apscheduler

RUN (development):
  uvicorn main:app --reload --port 8000

RUN (production):
  uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4

DEPLOY:
  Railway / Render / Fly.io — push this file + requirements.txt
  Set environment variables in their dashboard (see .env.example)
"""

import os
import json
import hmac
import hashlib
import httpx
import logging
from datetime import datetime, timezone
from typing import Optional, List
from functools import lru_cache

from fastapi import FastAPI, HTTPException, Depends, Header, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
from supabase import create_client, Client
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

# ── CONFIG ────────────────────────────────────────────────────────────────────
SUPABASE_URL      = os.environ["SUPABASE_URL"]
SUPABASE_KEY      = os.environ["SUPABASE_SERVICE_KEY"]   # Service role key (never expose to frontend)
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]
LS_API_KEY         = os.environ["LEMONSQUEEZY_API_KEY"]       # your Lemon Squeezy API key
LS_WEBHOOK_SECRET  = os.environ["LEMONSQUEEZY_WEBHOOK_SECRET"] # from LS Dashboard → Webhooks
LS_STORE_ID        = os.environ["LEMONSQUEEZY_STORE_ID"]       # your store numeric ID
FRONTEND_URL       = os.environ.get("FRONTEND_URL", "http://localhost:3000")

# Lemon Squeezy variant IDs — one per plan
# Find in: LS Dashboard → Products → your product → click variant → copy ID from URL
LS_VARIANTS = {
    "pro":           os.environ.get("LS_VARIANT_PRO",  "000000"),  # $19/month variant
    "institutional": os.environ.get("LS_VARIANT_INST", "000001"),  # $99/month variant
}

LS_API = "https://api.lemonsqueezy.com/v1"

CT_GOV_API  = "https://clinicaltrials.gov/api/v2/studies"
ISRCTN_API  = "https://www.isrctn.com/api/query/format/who"
EU_CTIS_API = "https://euclinicaltrials.eu/ctis-public/rest/retrieve"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("trialvault")

# ── LEMON SQUEEZY HELPER ──────────────────────────────────────────────────────
def ls_headers():
    return {
        "Authorization": f"Bearer {LS_API_KEY}",
        "Content-Type":  "application/vnd.api+json",
        "Accept":        "application/vnd.api+json",
    }

async def ls_post(path: str, body: dict):
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{LS_API}{path}", json=body, headers=ls_headers())
        r.raise_for_status()
        return r.json()

async def ls_get(path: str):
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{LS_API}{path}", headers=ls_headers())
        r.raise_for_status()
        return r.json()

# ── SUPABASE CLIENT ───────────────────────────────────────────────────────────
@lru_cache()
def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

# ── FASTAPI APP ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="TrialVault API",
    version="1.0.0",
    description="Backend API for TrialVault clinical trial discovery platform"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://localhost:3000", "http://127.0.0.1:5500"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── AUTH HELPERS ──────────────────────────────────────────────────────────────
async def get_current_user(authorization: Optional[str] = Header(None)):
    """Validate Supabase JWT token from Authorization header."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")

    token = authorization.split(" ")[1]
    sb = get_supabase()

    try:
        # Verify JWT with Supabase
        user = sb.auth.get_user(token)
        if not user or not user.user:
            raise HTTPException(status_code=401, detail="Invalid token")
        return user.user
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Auth error: {str(e)}")


async def get_profile(user=Depends(get_current_user)):
    """Fetch user profile from database."""
    sb = get_supabase()
    result = sb.table("profiles").select("*").eq("id", str(user.id)).single().execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Profile not found")
    return result.data


# ── PYDANTIC MODELS ───────────────────────────────────────────────────────────
class SearchRequest(BaseModel):
    query: str = ""
    registry: str = "ctgov"          # ctgov | isrctn | euctr | all | cached
    status: Optional[str] = None
    phase: Optional[str] = None
    study_type: Optional[str] = None
    location: Optional[str] = None
    page_size: int = 20
    page_token: Optional[str] = None  # for CT.gov pagination

class SaveTrialRequest(BaseModel):
    trial_id: str
    registry: str
    title: str
    status: Optional[str] = None
    phase: Optional[str] = None
    conditions: Optional[List[str]] = []
    sponsor: Optional[str] = None
    url: Optional[str] = None
    notes: Optional[str] = None

class AlertRequest(BaseModel):
    label: str
    query: str
    filters: dict = {}

class UpgradeRequest(BaseModel):
    plan: str           # 'pro' | 'institutional'
    redirect_url: str   # URL to return to after checkout

class ProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    profession: Optional[str] = None
    institution: Optional[str] = None


# ── HEALTH ────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "service": "TrialVault API", "version": "1.0.0"}

@app.get("/health")
def health():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


# ── PROFILE ───────────────────────────────────────────────────────────────────
@app.get("/api/profile")
async def read_profile(profile=Depends(get_profile)):
    """Get current user's profile and plan info."""
    return {
        "id": profile["id"],
        "email": profile["email"],
        "full_name": profile["full_name"],
        "profession": profile["profession"],
        "institution": profile["institution"],
        "plan": profile["plan"],
        "searches_today": profile["searches_today"],
        "plan_expires_at": profile["plan_expires_at"],
    }

@app.patch("/api/profile")
async def update_profile(body: ProfileUpdate, profile=Depends(get_profile)):
    """Update user profile fields."""
    sb = get_supabase()
    updates = {k: v for k, v in body.dict().items() if v is not None}
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    sb.table("profiles").update(updates).eq("id", profile["id"]).execute()
    return {"success": True}


# ── SEARCH ────────────────────────────────────────────────────────────────────
@app.post("/api/search")
async def search_trials(body: SearchRequest, profile=Depends(get_profile)):
    """
    Unified search endpoint — handles CT.gov, ISRCTN, EU CTIS, and cached registries.
    Enforces plan search limits via Supabase RPC.
    """
    sb = get_supabase()
    plan = profile["plan"]
    plan_limits = {"free": 3, "pro": 999999, "institutional": 999999}
    limit = plan_limits.get(plan, 3)
    page_size = min(body.page_size, {"free": 20, "pro": 100, "institutional": 500}.get(plan, 20))

    # Check and increment search count
    check = sb.rpc("increment_search", {"p_user_id": profile["id"]}).execute()
    if check.data and not check.data[0]["allowed"]:
        raise HTTPException(status_code=429, detail={
            "error": "daily_limit_reached",
            "message": f"You've used all {limit} daily searches on the free plan. Upgrade for unlimited.",
            "plan": plan
        })

    # Log the search
    sb.table("search_logs").insert({
        "user_id": profile["id"],
        "query": body.query,
        "registry": body.registry,
        "filters": {"status": body.status, "phase": body.phase, "location": body.location}
    }).execute()

    # Route to correct registry
    if body.registry == "ctgov":
        return await _search_ctgov(body, page_size)
    elif body.registry == "isrctn":
        return await _search_isrctn(body, page_size)
    elif body.registry == "euctr":
        return await _search_euctr(body, page_size)
    elif body.registry == "cached":
        return await _search_cached(body, page_size, sb)
    elif body.registry == "all":
        return await _search_all(body, page_size, sb)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown registry: {body.registry}")


async def _search_ctgov(body: SearchRequest, page_size: int):
    params = {"pageSize": page_size, "countTotal": "true",
              "fields": "NCTId,BriefTitle,OverallStatus,Phase,Conditions,MinimumAge,MaximumAge,Sex,LeadSponsor,LocationCountry,EnrollmentCount,StartDate"}
    if body.query:    params["query.cond"] = body.query
    if body.status:   params["filter.overallStatus"] = body.status
    if body.phase:    params["filter.phase"] = body.phase
    if body.location: params["query.locn"] = body.location
    if body.page_token: params["pageToken"] = body.page_token

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(CT_GOV_API, params=params)
        resp.raise_for_status()
        data = resp.json()

    return {
        "registry": "ctgov",
        "total": data.get("totalCount", 0),
        "next_page_token": data.get("nextPageToken"),
        "trials": data.get("studies", [])
    }


async def _search_isrctn(body: SearchRequest, page_size: int):
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(ISRCTN_API, params={"q": body.query, "limit": page_size})
        resp.raise_for_status()
    # Parse XML and return normalised list
    from lxml import etree
    root = etree.fromstring(resp.content)
    trials = []
    for t in root.findall(".//trial"):
        def g(tag): el = t.find(tag); return el.text.strip() if el is not None and el.text else ""
        trials.append({
            "id": g("TrialID"), "title": g("Public_title") or g("Scientific_title"),
            "status": g("Recruitment_status"), "phase": g("Phase"),
            "conditions": [g("Condition")], "min_age": g("Agemin"), "max_age": g("Agemax"),
            "gender": g("Gender"), "sponsor": g("Primary_sponsor"),
            "countries": [g("Countries")], "date_registered": g("Date_registration"),
            "url": f"https://www.isrctn.com/{g('TrialID')}", "_source": "ISRCTN"
        })
    return {"registry": "isrctn", "total": len(trials), "trials": trials}


async def _search_euctr(body: SearchRequest, page_size: int):
    payload = {
        "pagination": {"page": 0, "size": page_size},
        "sort": {"property": "dateOfCreation", "isAscending": False},
        "searchCriteria": {"containAll": body.query} if body.query else {}
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(EU_CTIS_API, json=payload)
        resp.raise_for_status()
        data = resp.json()
    items = data.get("data", data.get("results", []))
    trials = [{
        "id": t.get("ctNumber",""), "title": t.get("ctTitle","Untitled"),
        "status": t.get("trialStatus",""), "phase": t.get("trialPhase",""),
        "conditions": t.get("conditions",[]), "sponsor": t.get("sponsorName",""),
        "countries": t.get("memberStates",[]), "date_registered": t.get("dateOfCreation",""),
        "url": f"https://euclinicaltrials.eu/search-for-clinical-trials/?lang=en&eudraCTNumber={t.get('ctNumber','')}",
        "_source": "EUCTR"
    } for t in items]
    return {"registry": "euctr", "total": len(trials), "trials": trials}


async def _search_cached(body: SearchRequest, page_size: int, sb: Client):
    """Search the locally synced registry cache in Supabase."""
    result = sb.rpc("search_cached_trials", {
        "p_query": body.query or None,
        "p_registry": None,
        "p_status": body.status or None,
        "p_limit": page_size,
        "p_offset": 0
    }).execute()
    return {"registry": "cached", "total": len(result.data or []), "trials": result.data or []}


async def _search_all(body: SearchRequest, page_size: int, sb: Client):
    """Parallel search across CT.gov + cached registries."""
    import asyncio
    ctgov_task  = asyncio.create_task(_search_ctgov(body, min(page_size, 20)))
    cached_task = asyncio.create_task(_search_cached(body, min(page_size, 20), sb))
    ctgov_res, cached_res = await asyncio.gather(ctgov_task, cached_task, return_exceptions=True)

    trials = []
    if isinstance(ctgov_res, dict):  trials.extend(ctgov_res.get("trials", []))
    if isinstance(cached_res, dict): trials.extend(cached_res.get("trials", []))

    return {"registry": "all", "total": len(trials), "trials": trials}


# ── SAVED TRIALS ──────────────────────────────────────────────────────────────
@app.get("/api/saved")
async def list_saved(profile=Depends(get_profile)):
    sb = get_supabase()
    result = sb.table("saved_trials").select("*").eq("user_id", profile["id"]).order("saved_at", desc=True).execute()
    return {"trials": result.data or []}

@app.post("/api/saved")
async def save_trial(body: SaveTrialRequest, profile=Depends(get_profile)):
    sb = get_supabase()
    plan_limits = {"free": 5, "pro": 99999, "institutional": 99999}
    limit = plan_limits.get(profile["plan"], 5)

    count = sb.table("saved_trials").select("id", count="exact").eq("user_id", profile["id"]).execute()
    if (count.count or 0) >= limit:
        raise HTTPException(status_code=403, detail={
            "error": "save_limit_reached",
            "message": f"Free plan allows {limit} saved trials. Upgrade for unlimited."
        })

    sb.table("saved_trials").upsert({
        "user_id": profile["id"],
        **body.dict()
    }).execute()
    return {"success": True}

@app.delete("/api/saved/{trial_id}")
async def delete_saved(trial_id: str, profile=Depends(get_profile)):
    sb = get_supabase()
    sb.table("saved_trials").delete().eq("user_id", profile["id"]).eq("trial_id", trial_id).execute()
    return {"success": True}


# ── ALERTS ────────────────────────────────────────────────────────────────────
@app.get("/api/alerts")
async def list_alerts(profile=Depends(get_profile)):
    sb = get_supabase()
    result = sb.table("alerts").select("*").eq("user_id", profile["id"]).order("created_at", desc=True).execute()
    return {"alerts": result.data or []}

@app.post("/api/alerts")
async def create_alert(body: AlertRequest, profile=Depends(get_profile)):
    sb = get_supabase()
    plan_alert_limits = {"free": 0, "pro": 5, "institutional": 20}
    limit = plan_alert_limits.get(profile["plan"], 0)

    if limit == 0:
        raise HTTPException(status_code=403, detail={"error": "alerts_not_available", "message": "Upgrade to Pro to set alerts."})

    count = sb.table("alerts").select("id", count="exact").eq("user_id", profile["id"]).eq("active", True).execute()
    if (count.count or 0) >= limit:
        raise HTTPException(status_code=403, detail={"error": "alert_limit_reached", "message": f"You can set up to {limit} alerts on your plan."})

    sb.table("alerts").insert({"user_id": profile["id"], **body.dict()}).execute()
    return {"success": True}

@app.delete("/api/alerts/{alert_id}")
async def delete_alert(alert_id: str, profile=Depends(get_profile)):
    sb = get_supabase()
    sb.table("alerts").delete().eq("user_id", profile["id"]).eq("id", alert_id).execute()
    return {"success": True}


# ── LEMON SQUEEZY PAYMENTS ────────────────────────────────────────────────────
@app.post("/api/billing/checkout")
async def create_checkout(body: UpgradeRequest, profile=Depends(get_profile)):
    """
    Creates a Lemon Squeezy checkout session.
    Returns a checkout URL — open as overlay on frontend or redirect.

    Setup in Lemon Squeezy Dashboard:
      1. Products → Add Product → set price ($19 or $99), interval: monthly
      2. Copy the variant ID from the product page URL
      3. Set those IDs in your .env as LS_VARIANT_PRO and LS_VARIANT_INST
    """
    variant_id = LS_VARIANTS.get(body.plan)
    if not variant_id:
        raise HTTPException(status_code=400, detail="Invalid plan")

    result = await ls_post("/checkouts", {
        "data": {
            "type": "checkouts",
            "attributes": {
                "checkout_data": {
                    "email": profile["email"],
                    "name":  profile.get("full_name", ""),
                    "custom": {
                        "user_id": profile["id"],
                        "plan":    body.plan,
                    }
                },
                "checkout_options": {
                    "embed":         True,   # enables overlay mode
                    "media":         False,
                    "logo":          True,
                    "button_color":  "#6366f1",
                },
                "product_options": {
                    "redirect_url":       body.redirect_url,
                    "receipt_link_url":   body.redirect_url,
                    "receipt_thank_you_note": "Thank you for subscribing to TrialVault!",
                },
                "expires_at": None,    # no expiry
            },
            "relationships": {
                "store": {
                    "data": {"type": "stores", "id": str(LS_STORE_ID)}
                },
                "variant": {
                    "data": {"type": "variants", "id": str(variant_id)}
                }
            }
        }
    })

    checkout_url = result["data"]["attributes"]["url"]
    return {"checkout_url": checkout_url}


@app.post("/api/billing/cancel")
async def cancel_subscription(profile=Depends(get_profile)):
    """
    Cancels a Lemon Squeezy subscription.
    Subscription ID is stored in stripe_subscription_id column (reused for LS).
    """
    sub_id = profile.get("stripe_subscription_id")
    if not sub_id:
        raise HTTPException(status_code=400, detail="No active subscription found")

    await ls_post(f"/subscriptions/{sub_id}", {
        "data": {
            "type": "subscriptions",
            "id":   str(sub_id),
            "attributes": {"cancelled": True}
        }
    })

    sb = get_supabase()
    sb.table("profiles").update({"plan": "free"}).eq("id", profile["id"]).execute()
    log.info(f"⬇️ User {profile['id']} cancelled LS subscription {sub_id}")
    return {"success": True}


@app.get("/api/billing/portal/{subscription_id}")
async def billing_portal(subscription_id: str, profile=Depends(get_profile)):
    """Returns the Lemon Squeezy customer portal URL for managing the subscription."""
    result = await ls_get(f"/subscriptions/{subscription_id}")
    portal_url = result["data"]["attributes"].get("urls", {}).get("customer_portal")
    if not portal_url:
        raise HTTPException(status_code=404, detail="Portal URL not found")
    return {"portal_url": portal_url}


# ── LEMON SQUEEZY WEBHOOK ─────────────────────────────────────────────────────
@app.post("/api/webhooks/lemonsqueezy")
async def ls_webhook(request: Request):
    """
    Handles Lemon Squeezy webhook events to keep plans in sync.
    Set this URL in: LS Dashboard → Settings → Webhooks → Add webhook
      https://your-api.railway.app/api/webhooks/lemonsqueezy

    Events to enable:
      order_created, subscription_created, subscription_updated,
      subscription_cancelled, subscription_expired, subscription_payment_failed
    """
    payload    = await request.body()
    sig_header = request.headers.get("x-signature", "")

    # Verify HMAC-SHA256 signature
    expected = hmac.new(
        key=LS_WEBHOOK_SECRET.encode("utf-8"),
        msg=payload,
        digestmod=hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, sig_header):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event      = json.loads(payload)
    event_name = event.get("meta", {}).get("event_name", "")
    data       = event.get("data", {})
    attrs      = data.get("attributes", {})
    custom     = event.get("meta", {}).get("custom_data", {})

    sb = get_supabase()

    # Log event
    sb.table("stripe_events").upsert({
        "id":              data.get("id", f"ls_{int(datetime.now().timestamp())}"),
        "type":            event_name,
        "customer_id":     str(attrs.get("customer_id", "")),
        "subscription_id": str(data.get("id", "")),
        "data":            attrs,
    }).execute()

    user_id = custom.get("user_id")
    plan    = custom.get("plan", "pro")

    if event_name in ("order_created", "subscription_created"):
        # New subscription — upgrade the user
        if user_id:
            sb.table("profiles").update({
                "plan":                   plan,
                "stripe_subscription_id": str(data.get("id", "")),
                "stripe_customer_id":     str(attrs.get("customer_id", "")),
            }).eq("id", user_id).execute()
            log.info(f"✅ LS webhook: user {user_id} → {plan}")

    elif event_name in ("subscription_cancelled", "subscription_expired"):
        # Subscription ended — downgrade to free
        sub_id = str(data.get("id", ""))
        p = sb.table("profiles").select("id").eq("stripe_subscription_id", sub_id).maybe_single().execute()
        if p and p.data:
            sb.table("profiles").update({"plan": "free"}).eq("id", p.data["id"]).execute()
            log.info(f"⬇️ LS webhook: user {p.data['id']} downgraded (subscription ended)")

    elif event_name == "subscription_updated":
        # Handle plan changes or pauses
        status = attrs.get("status")
        sub_id = str(data.get("id", ""))
        if status not in ("active", "on_trial"):
            p = sb.table("profiles").select("id").eq("stripe_subscription_id", sub_id).maybe_single().execute()
            if p and p.data:
                sb.table("profiles").update({"plan": "free"}).eq("id", p.data["id"]).execute()

    elif event_name == "subscription_payment_failed":
        log.warning(f"⚠️ LS payment failed for subscription {data.get('id')}")

    return {"received": True}


# ── BACKGROUND ALERT CHECKER ──────────────────────────────────────────────────
async def check_and_send_alerts():
    """
    Runs daily — checks all active alerts and notifies users of new trials.
    In production, plug in SendGrid/Resend/Postmark for emails.
    """
    sb = get_supabase()
    alerts = sb.table("alerts").select("*, profiles(email, full_name)").eq("active", True).execute()

    for alert in (alerts.data or []):
        try:
            # Search CT.gov for current trial count
            params = {"query.cond": alert["query"], "pageSize": 1, "countTotal": "true"}
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(CT_GOV_API, params=params)
                data = resp.json()
                current_count = data.get("totalCount", 0)

            if current_count > alert.get("trial_count", 0):
                new_trials = current_count - (alert.get("trial_count") or 0)
                # TODO: Send email via SendGrid/Resend:
                # send_email(
                #   to=alert["profiles"]["email"],
                #   subject=f"TrialVault: {new_trials} new trials for '{alert['query']}'",
                #   body=f"Your alert '{alert['label']}' matched {current_count} trials..."
                # )
                log.info(f"Alert {alert['id']}: {new_trials} new trials for '{alert['query']}'")

                sb.table("alerts").update({
                    "trial_count": current_count,
                    "last_sent": datetime.now(timezone.utc).isoformat()
                }).eq("id", alert["id"]).execute()

        except Exception as e:
            log.error(f"Alert check error for {alert['id']}: {e}")


# ── SCHEDULER ─────────────────────────────────────────────────────────────────
scheduler = AsyncIOScheduler()

@app.on_event("startup")
async def startup():
    scheduler.add_job(check_and_send_alerts, "cron", hour=7, minute=0)  # 07:00 daily
    # Reset daily search counts at midnight
    scheduler.add_job(
        lambda: get_supabase().rpc("reset_daily_searches", {}).execute(),
        "cron", hour=0, minute=1
    )
    scheduler.start()
    log.info("TrialVault API started. Scheduler running.")

@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()
