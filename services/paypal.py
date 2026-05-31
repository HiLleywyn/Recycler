"""
services/paypal.py  -  PayPal Subscriptions API client for Discoin Premium.

Wraps the v1 Billing Subscriptions endpoints we actually need:

    - get_access_token    : OAuth2 client_credentials, cached till expiry
    - create_subscription : returns the approval link the user clicks
    - get_subscription    : status / next billing time / subscriber email
    - cancel_subscription : flip to CANCELLED (PayPal will fire a webhook)
    - verify_webhook      : signature verification through PayPal's API

Mode is selected by ``Config.PAYPAL_MODE`` -- 'sandbox' or 'live'. All
secrets come from env vars; nothing is committed. The bot module-imports
``paypal_client()`` lazily so a missing/empty client_id doesn't crash the
bot at startup -- features that touch PayPal will just return a friendly
"PayPal isn't configured" error.

Reference docs:
    https://developer.paypal.com/docs/api/subscriptions/v1/
    https://developer.paypal.com/docs/api/webhooks/v1/#verify-webhook-signature
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any, Optional

import aiohttp

from core.config import Config

log = logging.getLogger(__name__)

_API_BASE = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live":    "https://api-m.paypal.com",
}


class PayPalError(Exception):
    """Surfaced to user code when PayPal returns a non-2xx or is misconfigured."""

    def __init__(self, message: str, *, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class PayPalClient:
    """Singleton-style PayPal client. One instance per process is fine --
    aiohttp.ClientSession is lazily created on first use and reused for
    keep-alive."""

    def __init__(self) -> None:
        self.mode: str = (Config.PAYPAL_MODE or "sandbox").lower()
        self.base: str = _API_BASE.get(self.mode, _API_BASE["sandbox"])
        self.client_id: str = Config.PAYPAL_CLIENT_ID or ""
        self.client_secret: str = Config.PAYPAL_CLIENT_SECRET or ""
        self.webhook_id: str = Config.PAYPAL_WEBHOOK_ID or ""
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        """True if we have enough credentials to talk to PayPal."""
        return bool(self.client_id and self.client_secret)

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                timeout = aiohttp.ClientTimeout(total=20)
                self._session = aiohttp.ClientSession(timeout=timeout)
            return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ── auth ──────────────────────────────────────────────────────

    async def _get_token(self) -> str:
        if not self.configured:
            raise PayPalError("PayPal is not configured (missing client_id/secret)")
        async with self._token_lock:
            now = time.time()
            if self._token and self._token_expires_at > now + 30:
                return self._token
            sess = await self._get_session()
            auth = base64.b64encode(
                f"{self.client_id}:{self.client_secret}".encode()
            ).decode()
            headers = {
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            }
            async with sess.post(
                f"{self.base}/v1/oauth2/token",
                headers=headers,
                data="grant_type=client_credentials",
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise PayPalError(
                        f"PayPal token request failed: {resp.status}",
                        status=resp.status, body=text,
                    )
                data = json.loads(text)
            self._token = data["access_token"]
            # PayPal returns expires_in in seconds (~32400). Subtract a
            # safety margin so we re-auth before the token actually dies.
            self._token_expires_at = now + int(data.get("expires_in", 3600)) - 60
            return self._token

    # ── low-level request ─────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None,
        expect_empty: bool = False,
    ) -> dict[str, Any]:
        token = await self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        sess = await self._get_session()
        async with sess.request(
            method,
            f"{self.base}{path}",
            headers=headers,
            json=json_body,
        ) as resp:
            text = await resp.text()
            if resp.status >= 400:
                log.error("paypal %s %s -> %s %s", method, path, resp.status, text[:500])
                raise PayPalError(
                    f"PayPal {method} {path} -> {resp.status}",
                    status=resp.status, body=text,
                )
            if expect_empty or not text.strip():
                return {}
            return json.loads(text)

    # ── subscriptions ─────────────────────────────────────────────

    async def create_subscription(
        self,
        plan_id: str,
        *,
        custom_id: str,
        return_url: str,
        cancel_url: str,
        subscriber_email: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a subscription in APPROVAL_PENDING. Returns the full
        response which contains the approve link in ``links`` rel='approve'.
        ``custom_id`` is echoed back on every webhook -- we put the guild_id
        there so the webhook handler knows which guild to unlock."""
        if not plan_id:
            raise PayPalError("PayPal plan_id not configured")
        body: dict[str, Any] = {
            "plan_id": plan_id,
            "custom_id": custom_id,
            "application_context": {
                "brand_name": "Discoin",
                "user_action": "SUBSCRIBE_NOW",
                "return_url": return_url,
                "cancel_url": cancel_url,
            },
        }
        if subscriber_email:
            body["subscriber"] = {"email_address": subscriber_email}
        return await self._request("POST", "/v1/billing/subscriptions", json_body=body)

    async def get_subscription(self, sub_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/billing/subscriptions/{sub_id}")

    async def cancel_subscription(
        self,
        sub_id: str,
        *,
        reason: str = "Cancelled by Discoin",
    ) -> None:
        await self._request(
            "POST",
            f"/v1/billing/subscriptions/{sub_id}/cancel",
            json_body={"reason": reason[:128]},
            expect_empty=True,
        )

    # ── webhook signature verification ────────────────────────────

    async def verify_webhook(
        self,
        headers: dict[str, str],
        raw_body: str,
    ) -> bool:
        """Returns True iff PayPal confirms the signature is valid.

        Caller passes the raw request body (NOT json.loads'd) and a dict
        of HTTP headers (case-insensitive). When PAYPAL_WEBHOOK_ID is
        unset we fail closed -- never accept unverified webhooks."""
        if not self.webhook_id:
            log.warning("paypal.verify_webhook called with no PAYPAL_WEBHOOK_ID -- rejecting")
            return False

        def h(name: str) -> str:
            for k, v in headers.items():
                if k.lower() == name.lower():
                    return v
            return ""

        try:
            event_obj = json.loads(raw_body)
        except json.JSONDecodeError:
            return False

        payload = {
            "auth_algo":         h("PayPal-Auth-Algo"),
            "cert_url":          h("PayPal-Cert-Url"),
            "transmission_id":   h("PayPal-Transmission-Id"),
            "transmission_sig":  h("PayPal-Transmission-Sig"),
            "transmission_time": h("PayPal-Transmission-Time"),
            "webhook_id":        self.webhook_id,
            "webhook_event":     event_obj,
        }
        try:
            result = await self._request(
                "POST",
                "/v1/notifications/verify-webhook-signature",
                json_body=payload,
            )
        except PayPalError:
            log.exception("paypal.verify_webhook API call failed")
            return False
        return result.get("verification_status") == "SUCCESS"


# ── module-level singleton ────────────────────────────────────────

_client: Optional[PayPalClient] = None


def paypal_client() -> PayPalClient:
    """Lazy singleton. Creates the client on first access so importing
    this module never touches the network."""
    global _client
    if _client is None:
        _client = PayPalClient()
    return _client


def find_approve_link(subscription_response: dict[str, Any]) -> Optional[str]:
    """Pluck the rel='approve' href out of a Subscriptions create response."""
    for link in subscription_response.get("links") or []:
        if (link.get("rel") or "").lower() == "approve":
            return link.get("href")
    return None


def parse_iso8601_to_epoch(value: Optional[str]) -> Optional[float]:
    """PayPal returns ISO8601 strings like '2026-06-15T00:00:00Z'.
    asyncpg's to_timestamp() wants epoch seconds, so convert here."""
    if not value:
        return None
    from datetime import datetime
    s = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.timestamp()
