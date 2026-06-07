"""Sendivo SMS runtime — direct HTTP against app.sendivo.io/api/v1.

Earlier prototype used a subprocess wrapper around `sendivo-pp-cli`, but the
CLI binary isn't installed everywhere. Going direct HTTP makes this portable
(macOS, Linux VPS, anywhere).

Routes by tag:
  sms / sms-send / sendivo-send → POST /sms (send a message)
  sms-logs / sendivo-logs       → GET  /sms/logs
  sms-billing                   → GET  /billing/report

Auth: Bearer SENDIVO_BEARER_AUTH header.
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

_BEARER = os.getenv("SENDIVO_BEARER_AUTH", "")
_BASE_URL = os.getenv("SENDIVO_BASE_URL", "https://app.sendivo.io/api/v1")


async def run(job: Job) -> dict[str, Any]:
    task_id = str(uuid.uuid4())
    t0 = time.monotonic()
    tags = {tag.lower() for tag in job.tags}
    sdr = job.metadata.get("sdr_name", "unknown")

    publish_event("agents.sdr.sms.started", {
        "task_id": task_id,
        "sdr": sdr,
        "tags": list(tags),
    })

    if tags & {"sms", "sms-send", "sendivo-send"}:
        result = await _send_sms(job, task_id, sdr, t0)
    elif tags & {"sms-logs", "sendivo-logs"}:
        result = await _get_logs(job, task_id, sdr, t0)
    elif tags & {"sms-billing", "sendivo-billing"}:
        result = await _billing(job, task_id, sdr, t0)
    else:
        result = _error(task_id, f"sendivo: no matching tag in {tags!r}", t0)

    event = "agents.sdr.sms.completed" if result["status"] == "completed" \
        else "agents.sdr.sms.failed"
    publish_event(event, {
        "task_id": task_id,
        "sdr": sdr,
        "elapsed_seconds": result["elapsed_seconds"],
    })
    return result


def _headers() -> dict[str, str]:
    # Realistic User-Agent is REQUIRED — Sendivo sits behind Cloudflare with
    # bot protection that returns 1010 Forbidden for default Python/httpx UAs.
    return {
        "Authorization": f"Bearer {_BEARER}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15",
    }


async def _send_sms(job: Job, task_id: str, sdr: str, t0: float) -> dict[str, Any]:
    if not _BEARER:
        return _stub(task_id, "send", t0, sdr=sdr)

    to = job.metadata.get("phone_number")
    body = job.metadata.get("sms_body") or job.prompt
    if not to:
        return _error(task_id, "sendivo: phone_number required", t0)
    if not body:
        return _error(task_id, "sendivo: sms_body or job.prompt required", t0)

    payload: dict[str, Any] = {"to": to, "message": body}
    if from_id := job.metadata.get("sendivo_phone_number_id"):
        payload["fromPhoneNumberId"] = from_id
    if campaign := job.metadata.get("sendivo_campaign_id"):
        payload["campaignId"] = campaign

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(f"{_BASE_URL}/sms", headers=_headers(), json=payload)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPStatusError as e:
            return _error(task_id, f"sendivo send failed ({e.response.status_code}): {e.response.text[:200]}", t0)
        except httpx.HTTPError as e:
            return _error(task_id, f"sendivo send failed: {e}", t0)

    return {
        "status": "completed",
        "task_id": task_id,
        "channel": "sms-send",
        "sdr": sdr,
        "to": to,
        "sendivo_response": data,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
    }


async def _get_logs(job: Job, task_id: str, sdr: str, t0: float) -> dict[str, Any]:
    if not _BEARER:
        return _stub(task_id, "logs", t0, sdr=sdr)

    params: dict[str, Any] = {}
    if s := job.metadata.get("start_date"):
        params["startDate"] = s
    if e := job.metadata.get("end_date"):
        params["endDate"] = e
    if limit := job.metadata.get("limit"):
        params["limit"] = limit

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.get(f"{_BASE_URL}/sms/logs", headers=_headers(), params=params)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            return _error(task_id, f"sendivo logs failed: {e}", t0)

    return {
        "status": "completed",
        "task_id": task_id,
        "channel": "sms-logs",
        "sdr": sdr,
        "logs": data,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
    }


async def _billing(job: Job, task_id: str, sdr: str, t0: float) -> dict[str, Any]:
    if not _BEARER:
        return _stub(task_id, "billing", t0, sdr=sdr)

    params: dict[str, Any] = {}
    if s := job.metadata.get("start_date"):
        params["startDate"] = s
    if e := job.metadata.get("end_date"):
        params["endDate"] = e

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.get(f"{_BASE_URL}/billing/report", headers=_headers(), params=params)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            return _error(task_id, f"sendivo billing failed: {e}", t0)

    return {
        "status": "completed",
        "task_id": task_id,
        "channel": "sms-billing",
        "sdr": sdr,
        "report": data,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
    }


def _stub(task_id: str, channel: str, t0: float, **extra) -> dict[str, Any]:
    logger.warning("SENDIVO_BEARER_AUTH not set — returning dev stub for %s", channel)
    return {
        "status": "completed",
        "task_id": task_id,
        "channel": f"sms-{channel}",
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
