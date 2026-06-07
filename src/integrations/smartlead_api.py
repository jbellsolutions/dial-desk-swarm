"""Smartlead API wrapper — standalone, no agent_os dependencies.

Simplified from Super Agent's smartlead runtime for direct use in
Dial Desk Swarm's coordinator. Uses httpx only; stubs when SMARTLEAD_API_KEY
is missing so local dev doesn't break.

Endpoints covered:
  - send_to_sequence(campaign_id, lead_email, ...) → queue lead into campaign
  - poll_replies(campaign_id) → fetch replied events
  - get_stats(campaign_id) → analytics by date
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_API_KEY = os.getenv("SMARTLEAD_API_KEY", "")
_BASE_URL = os.getenv("SMARTLEAD_BASE_URL", "https://server.smartlead.ai/api/v1")


# ── Public helpers ─────────────────────────────────────────────────────────

async def send_to_sequence(
    campaign_id: str,
    lead_email: str,
    lead_first_name: str = "",
    lead_company: str = "",
    sdr_name: str = "dialdesk",
    icp_tier: str = "",
) -> dict[str, Any]:
    """Queue a single lead into a Smartlead campaign sequence."""
    task_id = str(uuid.uuid4())
    t0 = time.monotonic()

    if not _API_KEY:
        logger.warning("SMARTLEAD_API_KEY not set — stubbing email send")
        return _stub(task_id, "send", t0, campaign_id=campaign_id, lead_email=lead_email)

    payload = {
        "lead_list": [{
            "email": lead_email,
            "first_name": lead_first_name,
            "company_name": lead_company,
            "custom_fields": {
                "sdr_name": sdr_name,
                "icp_tier": icp_tier,
                "source": "dial_desk_swarm",
            },
        }],
    }

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(
                f"{_BASE_URL}/campaigns/{campaign_id}/leads",
                params={"api_key": _API_KEY},
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            return _error(task_id, f"smartlead send failed: {e}", t0)

    return {
        "status": "completed",
        "task_id": task_id,
        "channel": "email-send",
        "campaign_id": campaign_id,
        "lead_email": lead_email,
        "smartlead_response": data,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
    }


async def poll_replies(campaign_id: str, sdr_name: str = "dialdesk") -> dict[str, Any]:
    """Poll Smartlead for replied events on a campaign."""
    task_id = str(uuid.uuid4())
    t0 = time.monotonic()

    if not _API_KEY:
        return _stub(task_id, "poll", t0, campaign_id=campaign_id)

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.get(
                f"{_BASE_URL}/campaigns/{campaign_id}/statistics",
                params={"api_key": _API_KEY, "event_type": "REPLIED"},
            )
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            return _error(task_id, f"smartlead poll failed: {e}", t0)

    replies = data.get("data", []) if isinstance(data, dict) else []
    return {
        "status": "completed",
        "task_id": task_id,
        "channel": "email-poll",
        "campaign_id": campaign_id,
        "sdr": sdr_name,
        "reply_count": len(replies),
        "replies": replies,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
    }


async def get_stats(campaign_id: str) -> dict[str, Any]:
    """Fetch campaign analytics by date."""
    task_id = str(uuid.uuid4())
    t0 = time.monotonic()

    if not _API_KEY:
        return _stub(task_id, "stats", t0, campaign_id=campaign_id)

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.get(
                f"{_BASE_URL}/campaigns/{campaign_id}/analytics-by-date",
                params={"api_key": _API_KEY},
            )
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            return _error(task_id, f"smartlead stats failed: {e}", t0)

    return {
        "status": "completed",
        "task_id": task_id,
        "channel": "email-stats",
        "campaign_id": campaign_id,
        "stats": data,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
    }


# ── Internal ───────────────────────────────────────────────────────────────

def _stub(task_id: str, channel: str, t0: float, **extra) -> dict[str, Any]:
    return {
        "status": "completed",
        "task_id": task_id,
        "channel": f"email-{channel}",
        "stub": True,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
        **extra,
    }


def _error(task_id: str, msg: str, t0: float) -> dict[str, Any]:
    logger.error(msg)
    return {
        "status": "failed",
        "task_id": task_id,
        "error": msg,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
    }
