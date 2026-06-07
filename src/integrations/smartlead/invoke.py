"""Smartlead runtime invocation.

Routes by job tag:
  email-send / smartlead-send  → POST /campaigns/{id}/leads (queue lead into sequence)
  email-replies / smartlead-poll → GET /campaigns/{id}/leads/replies (fetch new replies)
  email-stats / smartlead-stats → GET /campaigns/{id}/statistics

Per-SDR pinning: every job MUST carry `metadata.sdr_name` and the runtime
resolves the SDR's pinned inbox/campaign from identity YAML before any send.
This guarantees inbound replies map back to the owning SDR and aren't seen
by other campaigns.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

import httpx

from agent_os.orchestrator.adapters.job_router import Job
from agent_os.bus.nats_publisher import publish_event

logger = logging.getLogger(__name__)

_API_KEY = os.getenv("SMARTLEAD_API_KEY", "")
_BASE_URL = os.getenv("SMARTLEAD_BASE_URL", "https://server.smartlead.ai/api/v1")


async def run(job: Job) -> dict[str, Any]:
    task_id = str(uuid.uuid4())
    t0 = time.monotonic()
    tags = {tag.lower() for tag in job.tags}
    sdr_name = job.metadata.get("sdr_name", "unknown")

    publish_event("agents.sdr.email.started", {
        "task_id": task_id,
        "sdr": sdr_name,
        "tags": list(tags),
    })

    if tags & {"email-send", "smartlead-send"}:
        result = await _send_to_sequence(job, task_id, sdr_name, t0)
    elif tags & {"email-replies", "smartlead-poll"}:
        result = await _poll_replies(job, task_id, sdr_name, t0)
    elif tags & {"email-stats", "smartlead-stats"}:
        result = await _get_stats(job, task_id, sdr_name, t0)
    else:
        result = _error(task_id, f"smartlead: no matching tag in {tags!r}", t0)

    event = "agents.sdr.email.completed" if result["status"] == "completed" \
        else "agents.sdr.email.failed"
    publish_event(event, {
        "task_id": task_id,
        "sdr": sdr_name,
        "elapsed_seconds": result["elapsed_seconds"],
    })
    return result


async def _send_to_sequence(job: Job, task_id: str, sdr: str, t0: float) -> dict[str, Any]:
    if not _API_KEY:
        return _stub(task_id, "send", t0, sdr=sdr)

    campaign_id = job.metadata.get("campaign_id")
    lead_email = job.metadata.get("lead_email")
    lead_first = job.metadata.get("lead_first_name", "")
    lead_company = job.metadata.get("lead_company", "")

    if not campaign_id or not lead_email:
        return _error(task_id, "smartlead: campaign_id and lead_email required", t0)

    payload = {
        "lead_list": [{
            "email": lead_email,
            "first_name": lead_first,
            "company_name": lead_company,
            "custom_fields": {
                "sdr_name": sdr,
                "icp_tier": job.metadata.get("icp_tier", ""),
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
        "sdr": sdr,
        "campaign_id": campaign_id,
        "lead_email": lead_email,
        "smartlead_response": data,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
    }


async def _poll_replies(job: Job, task_id: str, sdr: str, t0: float) -> dict[str, Any]:
    if not _API_KEY:
        return _stub(task_id, "poll", t0, sdr=sdr)

    campaign_id = job.metadata.get("campaign_id")
    if not campaign_id:
        return _error(task_id, "smartlead: campaign_id required for poll", t0)

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

    # Publish each reply as its own event so the SDR runtime can react fast.
    replies = data.get("data", []) if isinstance(data, dict) else []
    for reply in replies:
        publish_event("agents.sdr.email.replied", {
            "sdr": sdr,
            "campaign_id": campaign_id,
            "lead_email": reply.get("lead_email"),
            "reply_text": reply.get("reply_message", "")[:2000],
            "received_at": reply.get("event_timestamp"),
        })

    return {
        "status": "completed",
        "task_id": task_id,
        "channel": "email-poll",
        "sdr": sdr,
        "campaign_id": campaign_id,
        "reply_count": len(replies),
        "elapsed_seconds": round(time.monotonic() - t0, 3),
    }


async def _get_stats(job: Job, task_id: str, sdr: str, t0: float) -> dict[str, Any]:
    if not _API_KEY:
        return _stub(task_id, "stats", t0, sdr=sdr)

    campaign_id = job.metadata.get("campaign_id")
    if not campaign_id:
        return _error(task_id, "smartlead: campaign_id required for stats", t0)

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
        "sdr": sdr,
        "campaign_id": campaign_id,
        "stats": data,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
    }


def _stub(task_id: str, channel: str, t0: float, **extra) -> dict[str, Any]:
    logger.warning("SMARTLEAD_API_KEY not set — returning dev stub for %s", channel)
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
