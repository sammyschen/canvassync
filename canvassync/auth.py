"""Persistent browser session for Canvas SSO login."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager

from playwright.sync_api import BrowserContext, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from .client import parse_json
from .config import PROFILE_DIR

log = logging.getLogger("canvassync")

_ME_ENDPOINT = "/api/v1/users/self"


@contextmanager
def browser(headless: bool = True):
    """Yield a persistent BrowserContext whose cookies survive between runs."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(PROFILE_DIR), headless=headless)
        try:
            yield ctx
        finally:
            ctx.close()


def current_user(ctx: BrowserContext, base_url: str) -> dict | None:
    """Return the logged-in user's profile, or None if the session is invalid."""
    try:
        resp = ctx.request.get(base_url + _ME_ENDPOINT, headers={"Accept": "application/json"})
    except PlaywrightError as exc:
        log.debug("users/self request failed: %s", exc)
        return None
    if not resp.ok:
        return None
    try:
        data = parse_json(resp.text())
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("id") else None


def ensure_session(ctx: BrowserContext, base_url: str, *, interactive: bool = False) -> dict | None:
    """Validate the Canvas session, refreshing it via SSO if possible.

    Non-interactive mode still navigates to Canvas once: if the SSO cookies
    are alive, Canvas re-issues its session cookie silently, which stretches
    one manual login across weeks of headless runs.

    Rather than matching redirect URLs (which differ per identity provider),
    we poll the API until it accepts the session or the timeout runs out.
    """
    user = current_user(ctx, base_url)
    if user:
        return user

    page = ctx.new_page()
    try:
        try:
            page.goto(base_url + "/", wait_until="domcontentloaded")
        except PlaywrightError as exc:
            log.debug("navigation to %s failed: %s", base_url, exc)
        deadline = time.monotonic() + (300 if interactive else 30)
        while time.monotonic() < deadline:
            page.wait_for_timeout(2_000)
            user = current_user(ctx, base_url)
            if user:
                return user
            if page.is_closed():
                break
        return None
    finally:
        if not page.is_closed():
            page.close()
