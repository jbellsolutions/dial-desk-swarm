"""Sendivo SMS API wrapper — standalone, no agent_os dependencies.

Simplified from Super Agent's sendivo runtime for direct use in
Dial Desk Swarm's coordinator. Uses httpx only; stubs when SENDIVO_BEARER_AUTH
is missing so local dev doesn't break.

Cloudflare bot protection note: Sendivo requires a realistic User-Agent.
This wrapper hardcodes a Safari macOS UA to bypass 1010 Forbidden.

Endpoints covered:
  - send_sms(to_number, body, ...) → POST /sms
  - get_logs(start_date, end_date, limit) → GET /sms/logs
  - get_billing(start_date, end_date) → GET /billing/report
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BEARER = os.getenv("SENDIVO_BEARER_AUTH", "")
_BASE_URL = os.getenv("SENDIVO_BASE_URL", "https://app.sendivo.io/api/v1")

_HEADERS = {
    "Authorization": f"Bearer {_BEARER}",
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15"
    ),
}


# ── Public helpers ─────────────────────────────────────────────────────────

async def send_sms(
    to: str,
    body: str,
    from_phone_id: str = "",
    campaign_id: str = "",
    sdr_name: str = "dialdesk",
) -> dict[str, Any]:
    """Send an SMS via Sendivo. Returns stub if SENDIVO_BEARER_AUTH missing."""
    task_id = str(uuid.uuid4())
    t0 = time.monotonic()

    if not _BEARER:
        logger.warning("SENDIVO_BEARER_AUTH not set — stubbing SMS send")
        return _stub(task_id, "send", t0, to=to, sdr=sdr_name)

    payload: dict[str, Any] = {"to": to, "message": body}
    if from_phone_id:
        payload["fromPhoneNumberId"] = from_phone_id
    if campaign_id:
        payload["campaignId"] = campaign_id

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.post(f"{_BASE_URL}/sms", headers=_HEADERS, json=payload)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPStatusError as e:
            return _error(
                task_id,
                f"sendivo send failed ({e.response.status_code}): {e.response.text[:200]}",
                t0,
            )
        except httpx.HTTPError as e:
            return _error(task_id, f"sendivo send failed: {e}", t0)

    return {
        "status": "completed",
        "task_id": task_id,
        "channel": "sms-send",
        "sdr": sdr_name,
        "to": to,
        "sendivo_response": data,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
    }


async def get_logs(
    start_date: str = "",
    end_date: str = "",
    limit: int = 100,
    sdr_name: str = "dialdesk",
) -> dict[str, Any]:
    """Fetch SMS logs from Sendivo."""
    task_id = str(uuid.uuid4())
    t0 = time.monotonic()

    if not _BEARER:
        return _stub(task_id, "logs", t0, sdr=sdr_name)

    params: dict[str, Any] = {}
    if start_date:
        params["startDate"] = start_date
    if end_date:
        params["endDate"] = end_date
    if limit:
        params["limit"] = limit

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.get(f"{_BASE_URL}/sms/logs", headers=_HEADERS, params=params)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            return _error(task_id, f"sendivo logs failed: {e}", t0)

    return {
        "status": "completed",
        "task_id": task_id,
        "channel": "sms-logs",
        "sdr": sdr_name,
        "logs": data,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
    }


async def get_billing(
    start_date: str = "",
    end_date: str = "",
    sdr_name: str = "dialdesk",
) -> dict[str, Any]:
    """Fetch billing report from Sendivo."""
    task_id = str(uuid.uuid4())
    t0 = time.monotonic()

    if not _BEARER:
        return _stub(task_id, "billing", t0, sdr=sdr_name)

    params: dict[str, Any] = {}
    if start_date:
        params["startDate"] = start_date
    if end_date:
        params["endDate"] = end_date

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            r = await client.get(f"{_BASE_URL}/billing/report", headers=_HEADERS, params=params)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            return _error(task_id, f"sendivo billing failed: {e}", t0)

    return {
        "status": "completed",
        "task_id": task_id,
        "channel": "sms-billing",
        "sdr": sdr_name,
        "report": data,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
    }


# ── Internal ───────────────────────────────────────────────────────────────

def _stub(task_id: str, channel: str, t0: float, **extra) -> dict[str, Any]:
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
