"""DialDesk browser/super-browser operator.

This process is intentionally separate from the FastAPI app. It can run on the
same VPS, a local desktop, or a larger browser automation box. It claims durable
browser jobs, keeps the lease alive, performs the browser work, and writes the
result back to DialDesk.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx


API_BASE_URL = os.getenv("DIALDESK_API_BASE_URL", "http://127.0.0.1:8080")
API_TOKEN = os.getenv("APP_TOKEN") or os.getenv("DIALDESK_API_TOKEN", "")
OPERATOR_ID = os.getenv("DIALDESK_BROWSER_OPERATOR_ID", "super-browser")
BROWSER_MODE = os.getenv("DIALDESK_BROWSER_MODE", "auto")
ARTIFACT_DIR = os.getenv("DIALDESK_BROWSER_ARTIFACT_DIR", "/app/data/browser-artifacts")
POLL_SECONDS = float(os.getenv("DIALDESK_BROWSER_POLL_SECONDS", "15"))
LEASE_SECONDS = int(os.getenv("DIALDESK_BROWSER_LEASE_SECONDS", "900"))
MAX_TEXT_CHARS = int(os.getenv("DIALDESK_BROWSER_MAX_TEXT_CHARS", "4000"))
OPERATOR_VERSION = "1.1"


def json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def clean_text(value: str, limit: int = MAX_TEXT_CHARS) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    return text[:limit]


def page_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html or "", flags=re.I | re.S)
    return clean_text(match.group(1), 300) if match else ""


def strip_html(html: str) -> str:
    text = re.sub(r"<script\b[^<]*(?:(?!</script>)<[^<]*)*</script>", " ", html or "", flags=re.I)
    text = re.sub(r"<style\b[^<]*(?:(?!</style>)<[^<]*)*</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return clean_text(text)


@dataclass
class BrowserResult:
    status: str
    result: str
    artifacts: list[dict[str, Any]]
    metadata: dict[str, Any]


class DialDeskBrowserApi:
    def __init__(self, base_url: str = API_BASE_URL, token: str = API_TOKEN, operator_id: str = OPERATOR_ID):
        self.base_url = base_url.rstrip("/")
        self.operator_id = operator_id
        headers = {"X-Operator": operator_id}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.Client(base_url=self.base_url, headers=headers, timeout=30)

    def close(self) -> None:
        self.client.close()

    def claim(self) -> dict[str, Any]:
        response = self.client.post(
            "/api/browser/jobs/claim",
            json={"operator_id": self.operator_id, "lease_seconds": LEASE_SECONDS},
        )
        response.raise_for_status()
        return response.json()

    def operator_heartbeat(
        self,
        status: str = "online",
        mode: str = BROWSER_MODE,
        current_job_id: str = "",
        note: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self.client.post(
            "/api/browser/operators/heartbeat",
            json={
                "operator_id": self.operator_id,
                "status": status,
                "mode": mode,
                "current_job_id": current_job_id,
                "version": OPERATOR_VERSION,
                "note": note,
                "metadata": metadata or {},
            },
        )
        response.raise_for_status()
        return response.json()

    def heartbeat(self, job_id: str, note: str = "") -> dict[str, Any]:
        response = self.client.post(
            f"/api/browser/jobs/{job_id}/heartbeat",
            json={"operator_id": self.operator_id, "lease_seconds": LEASE_SECONDS, "note": note},
        )
        response.raise_for_status()
        return response.json()

    def complete(self, job_id: str, result: BrowserResult) -> dict[str, Any]:
        response = self.client.post(
            f"/api/browser/jobs/{job_id}/complete",
            json={
                "status": result.status,
                "result": result.result,
                "artifacts": result.artifacts,
                "metadata": result.metadata,
            },
        )
        response.raise_for_status()
        return response.json()

    def fail(self, job_id: str, error: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.client.post(
            f"/api/browser/jobs/{job_id}/fail",
            json={
                "status": "failed",
                "result": error,
                "artifacts": [],
                "metadata": metadata or {},
            },
        )
        response.raise_for_status()
        return response.json()


async def inspect_with_playwright(job: dict[str, Any], artifact_dir: str) -> BrowserResult:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed. Install with `pip install playwright` and `playwright install chromium`.") from exc

    url = job.get("url") or ""
    if not url:
        return BrowserResult(
            status="completed",
            result=f"No URL supplied. Objective recorded for manual browser work: {job.get('objective', '')}",
            artifacts=[],
            metadata={"mode": "playwright", "url": "", "manual_required": True},
        )

    pathlib.Path(artifact_dir).mkdir(parents=True, exist_ok=True)
    screenshot_path = pathlib.Path(artifact_dir) / f"browser-job-{job['id']}.png"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1100})
        response = await page.goto(url, wait_until="networkidle", timeout=45000)
        title = await page.title()
        body = clean_text(await page.locator("body").inner_text(timeout=10000))
        await page.screenshot(path=str(screenshot_path), full_page=True)
        final_url = page.url
        status_code = response.status if response else None
        await browser.close()

    result = (
        f"Browser job completed with Playwright.\n\n"
        f"Objective: {job.get('objective')}\n"
        f"URL: {url}\n"
        f"Final URL: {final_url}\n"
        f"HTTP status: {status_code}\n"
        f"Title: {title}\n\n"
        f"Visible text excerpt:\n{body}"
    )
    return BrowserResult(
        status="completed",
        result=result,
        artifacts=[{"kind": "screenshot", "path": str(screenshot_path)}],
        metadata={"mode": "playwright", "url": url, "final_url": final_url, "status_code": status_code, "title": title},
    )


async def inspect_with_http(job: dict[str, Any]) -> BrowserResult:
    url = job.get("url") or ""
    if not url:
        return BrowserResult(
            status="completed",
            result=f"No URL supplied. Objective recorded for manual browser work: {job.get('objective', '')}",
            artifacts=[],
            metadata={"mode": "http", "url": "", "manual_required": True},
        )

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(url)
    content_type = response.headers.get("content-type", "")
    text = response.text
    title = page_title(text)
    excerpt = strip_html(text) if "html" in content_type.lower() else clean_text(text)
    result = (
        f"Browser job completed with HTTP inspector.\n\n"
        f"Objective: {job.get('objective')}\n"
        f"URL: {url}\n"
        f"Final URL: {response.url}\n"
        f"HTTP status: {response.status_code}\n"
        f"Content type: {content_type}\n"
        f"Title: {title or 'not found'}\n\n"
        f"Text excerpt:\n{excerpt}"
    )
    return BrowserResult(
        status="completed",
        result=result,
        artifacts=[],
        metadata={
            "mode": "http",
            "url": url,
            "final_url": str(response.url),
            "status_code": response.status_code,
            "content_type": content_type,
            "title": title,
        },
    )


async def inspect_job(job: dict[str, Any], mode: str = BROWSER_MODE, artifact_dir: str = ARTIFACT_DIR) -> BrowserResult:
    selected = (mode or "auto").lower()
    if selected == "playwright":
        return await inspect_with_playwright(job, artifact_dir)
    if selected == "http":
        return await inspect_with_http(job)
    try:
        return await inspect_with_playwright(job, artifact_dir)
    except Exception as exc:
        fallback = await inspect_with_http(job)
        fallback.metadata["playwright_fallback_reason"] = str(exc)
        fallback.result += f"\n\nPlaywright fallback reason: {exc}"
        return fallback


async def run_once(api: DialDeskBrowserApi, mode: str = BROWSER_MODE, artifact_dir: str = ARTIFACT_DIR) -> dict[str, Any]:
    api.operator_heartbeat("online", mode=mode, note="Browser operator polling for work.")
    claimed = api.claim()
    if not claimed.get("claimed"):
        api.operator_heartbeat("idle", mode=mode, note="No browser jobs available.")
        return {"claimed": False, "completed": False}
    job = claimed["job"]
    job_id = job["id"]
    try:
        api.operator_heartbeat("claimed", mode=mode, current_job_id=job_id, note="Browser job claimed.")
        api.heartbeat(job_id, "Browser operator claimed job and is starting.")
        api.operator_heartbeat("working", mode=mode, current_job_id=job_id, note="Browser inspection running.")
        result = await inspect_job(job, mode=mode, artifact_dir=artifact_dir)
        api.heartbeat(job_id, "Browser operator finished inspection and is saving results.")
        completed = api.complete(job_id, result)
        api.operator_heartbeat("completed", mode=mode, current_job_id=job_id, note="Browser job completed.", metadata={"status": result.status})
        return {"claimed": True, "completed": True, "job": completed}
    except Exception as exc:
        failed = api.fail(job_id, str(exc), {"mode": mode, "operator_id": api.operator_id})
        api.operator_heartbeat("failed", mode=mode, current_job_id=job_id, note=str(exc))
        return {"claimed": True, "completed": False, "job": failed, "error": str(exc)}


async def run_loop(api: DialDeskBrowserApi, mode: str, artifact_dir: str, poll_seconds: float) -> None:
    while True:
        result = await run_once(api, mode=mode, artifact_dir=artifact_dir)
        if not result.get("claimed"):
            await asyncio.sleep(poll_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the DialDesk browser/super-browser operator.")
    parser.add_argument("--api-base-url", default=API_BASE_URL)
    parser.add_argument("--operator-id", default=OPERATOR_ID)
    parser.add_argument("--token", default=API_TOKEN)
    parser.add_argument("--mode", choices=["auto", "playwright", "http"], default=BROWSER_MODE)
    parser.add_argument("--artifact-dir", default=ARTIFACT_DIR)
    parser.add_argument("--poll-seconds", type=float, default=POLL_SECONDS)
    parser.add_argument("--once", action="store_true", help="Claim and process at most one browser job.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    api = DialDeskBrowserApi(args.api_base_url, args.token, args.operator_id)
    try:
        if args.once:
            result = asyncio.run(run_once(api, mode=args.mode, artifact_dir=args.artifact_dir))
            print(json_dumps(result))
        else:
            print(f"DialDesk browser operator running as {args.operator_id} against {args.api_base_url}")
            asyncio.run(run_loop(api, args.mode, args.artifact_dir, args.poll_seconds))
    finally:
        api.close()


if __name__ == "__main__":
    main()
