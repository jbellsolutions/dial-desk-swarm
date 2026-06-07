"""Prospect intel runtime invocation.

Routes by tag:
  ingest-list / prospect-ingest → score CSV/list of prospects, emit per-row
                                   tier classification events
  enrich / prospect-enrich      → enrich one lead via Apollo/Exa/Apify
  score                         → score one lead (no enrichment)
"""
from __future__ import annotations

import csv
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from agent_os.orchestrator.adapters.job_router import Job
from agent_os.bus.nats_publisher import publish_event

from .scoring import ProspectSignals, score as score_prospect

logger = logging.getLogger(__name__)


async def run(job: Job) -> dict[str, Any]:
    task_id = str(uuid.uuid4())
    t0 = time.monotonic()
    tags = {tag.lower() for tag in job.tags}

    publish_event("agents.prospect_intel.started", {
        "task_id": task_id,
        "tags": list(tags),
    })

    if tags & {"ingest-list", "prospect-ingest"}:
        result = await _ingest(job, task_id, t0)
    elif tags & {"enrich", "prospect-enrich"}:
        result = await _enrich(job, task_id, t0)
    elif tags & {"score"}:
        result = await _score_one(job, task_id, t0)
    else:
        result = _error(task_id, f"prospect_intel: no matching tag in {tags!r}", t0)

    event = "agents.prospect_intel.completed" if result["status"] == "completed" \
        else "agents.prospect_intel.failed"
    publish_event(event, {
        "task_id": task_id,
        "elapsed_seconds": result["elapsed_seconds"],
    })
    return result


async def _ingest(job: Job, task_id: str, t0: float) -> dict[str, Any]:
    csv_path = job.metadata.get("csv_path")
    if not csv_path or not Path(csv_path).exists():
        return _error(task_id, f"prospect_intel: csv_path missing or not found: {csv_path}", t0)

    counts = {"A": 0, "B": 0, "C": 0}
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sigs = _signals_from_row(row)
            res = score_prospect(sigs)
            counts[res.tier] += 1
            publish_event("agents.prospect_intel.scored", {
                "lead_email": row.get("email"),
                "lead_company": row.get("company"),
                "score": res.score,
                "tier": res.tier,
                "breakdown": res.breakdown,
            })

    return {
        "status": "completed",
        "task_id": task_id,
        "channel": "ingest-list",
        "csv_path": csv_path,
        "tier_counts": counts,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
    }


async def _enrich(job: Job, task_id: str, t0: float) -> dict[str, Any]:
    # Real implementation hits Apollo, Apify, Exa. Stub returns the row as-is
    # with a flag so callers can short-circuit during dev.
    if not (os.getenv("APOLLO_API_KEY") or os.getenv("EXA_API_KEY") or os.getenv("APIFY_TOKEN")):
        logger.warning("No enrichment API keys set — enrich is a no-op stub")
        return {
            "status": "completed",
            "task_id": task_id,
            "channel": "enrich",
            "stub": True,
            "lead": dict(job.metadata),
            "elapsed_seconds": round(time.monotonic() - t0, 3),
        }
    # TODO: implement Apollo/Exa/Apify calls; emit `agents.prospect_intel.enriched` event
    return {
        "status": "completed",
        "task_id": task_id,
        "channel": "enrich",
        "note": "enrich provider integration not yet wired",
        "elapsed_seconds": round(time.monotonic() - t0, 3),
    }


async def _score_one(job: Job, task_id: str, t0: float) -> dict[str, Any]:
    sigs = _signals_from_row(job.metadata)
    res = score_prospect(sigs)
    return {
        "status": "completed",
        "task_id": task_id,
        "channel": "score",
        "score": res.score,
        "tier": res.tier,
        "breakdown": res.breakdown,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
    }


def _signals_from_row(row: dict[str, Any]) -> ProspectSignals:
    """Translate a flat CSV/metadata row into ProspectSignals (0..1 each).

    The row is expected to carry pre-computed match floats from upstream
    enrichment. If absent, this returns zeros — caller should enrich first.
    """
    def f(key: str) -> float:
        v = row.get(key)
        try:
            return max(0.0, min(1.0, float(v))) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    return ProspectSignals(
        industry_match=f("industry_match"),
        revenue_match=f("revenue_match"),
        headcount_match=f("headcount_match"),
        tech_stack_match=f("tech_stack_match"),
        title_match=f("title_match"),
        funding_match=f("funding_match"),
        raw=dict(row),
    )


def _error(task_id: str, msg: str, t0: float) -> dict[str, Any]:
    logger.error(msg)
    return {
        "status": "failed",
        "task_id": task_id,
        "error": msg,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
    }
