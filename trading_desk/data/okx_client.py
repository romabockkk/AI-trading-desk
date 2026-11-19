"""OKX v5 REST client — DEMO TRADING ONLY.

* Public endpoints need no credentials.
* Private endpoints are signed (HMAC-SHA256, base64) and ALWAYS carry
  ``x-simulated-trading: 1``. There is deliberately no switch to turn that off:
  this client cannot reach a live account.
* Secrets are never logged; errors are re-raised without request headers.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import requests


class OKXError(RuntimeError):
    def __init__(self, code: str, msg: str, path: str, data: Any = None):
        super().__init__(f"OKX error {code} on {path}: {msg}")
        self.code, self.msg, self.path, self.data = code, msg, path, data


class OKXClient:
    DEMO_HEADER = {"x-simulated-trading": "1"}

    def __init__(self, base_url: str = "https://www.okx.com", api_key: str | None = None,
                 secret: str | None = None, passphrase: str | None = None, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self._key, self._secret, self._pass = api_key, secret, passphrase
        self.timeout = timeout
        self.session = requests.Session()
        self._clock_offset_ms = 0
        self._last_call = 0.0

    # ------------------------------------------------------------------ core
    @property
    def can_sign(self) -> bool:
        return bool(self._key and self._secret and self._pass)

    def _throttle(self) -> None:
        # Stay well under OKX limits (most public endpoints: 20 req / 2 s).
        wait = 0.12 - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _timestamp(self) -> str:
        ms = int(time.time() * 1000) + self._clock_offset_ms
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"

    def _sign(self, ts: str, method: str, path: str, body: str) -> str:
        assert self._secret is not None
        mac = hmac.new(self._secret.encode(), f"{ts}{method}{path}{body}".encode(), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def request(self, method: str, path: str, params: dict | None = None,
                body: dict | None = None, private: bool = False, retries: int = 3) -> list[dict] | list:
        method = method.upper()
        query = ("?" + urlencode({k: v for k, v in (params or {}).items() if v is not None})) if params else ""
        req_path = path + query
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        last_exc: Exception | None = None
        for attempt in range(retries):
            headers = {"Content-Type": "application/json"}
            if private:
                if not self.can_sign:
                    raise OKXError("NO_CREDENTIALS", "OKX demo API credentials are not configured", path)
                ts = self._timestamp()
                headers.update({
                    "OK-ACCESS-KEY": self._key or "",
                    "OK-ACCESS-SIGN": self._sign(ts, method, req_path, body_str),
                    "OK-ACCESS-TIMESTAMP": ts,
                    "OK-ACCESS-PASSPHRASE": self._pass or "",
                    **self.DEMO_HEADER,
                })
            self._throttle()
            try:
                resp = self.session.request(method, self.base_url + req_path, data=body_str or None,
                                            headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:  # network error -> retry with backoff
                last_exc = exc
                time.sleep(0.5 * 2 ** attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = OKXError(str(resp.status_code), resp.text[:200], path)
                time.sleep(0.5 * 2 ** attempt)
                continue
            try:
                payload = resp.json()
            except ValueError:
                raise OKXError(str(resp.status_code), resp.text[:200], path) from None
            code = str(payload.get("code", ""))
            if code == "50102" and attempt + 1 < retries:  # timestamp expired -> resync clock
                self.sync_clock()
                continue
            if code != "0":
                raise OKXError(code, payload.get("msg", ""), path, payload.get("data"))
            return payload.get("data", [])
        raise OKXError("NETWORK", f"request failed after {retries} attempts: {last_exc}", path)

    def sync_clock(self) -> None:
        data = self.request("GET", "/api/v5/public/time")
        self._clock_offset_ms = int(data[0]["ts"]) - int(time.time() * 1000)

    # --------------------------------------------------------------- public
    def candles(self, inst_id: str, bar: str, limit: int = 300) -> list[list[str]]:
        """Most recent candles, newest first. Uses /market/candles (max 300)."""
        return self.request("GET", "/api/v5/market/candles",
                            {"instId": inst_id, "bar": bar, "limit": str(min(limit, 300))})

    def history_candles(self, inst_id: str, bar: str, after: str | None = None,
                        limit: int = 100) -> list[list[str]]:
        """Older candles (records earlier than ``after`` ts), newest first."""
        return self.request("GET", "/api/v5/market/history-candles",
                            {"instId": inst_id, "bar": bar, "after": after, "limit": str(min(limit, 100))})

    def instrument(self, inst_id: str, inst_type: str = "SWAP") -> dict:
        data = self.request("GET", "/api/v5/public/instruments", {"instType": inst_type, "instId": inst_id})
        if not data:
            raise OKXError("NOT_FOUND", f"instrument {inst_id} not found", "/api/v5/public/instruments")
        return data[0]

    def funding_rate(self, inst_id: str) -> dict:
        return self.request("GET", "/api/v5/public/funding-rate", {"instId": inst_id})[0]

    def open_interest(self, inst_id: str) -> dict:
        return self.request("GET", "/api/v5/public/open-interest",
                            {"instType": "SWAP", "instId": inst_id})[0]

    def ticker(self, inst_id: str) -> dict:
        return self.request("GET", "/api/v5/market/ticker", {"instId": inst_id})[0]

    def announcements(self, page: int = 1) -> list[dict]:
        data = self.request("GET", "/api/v5/support/announcements", {"page": str(page)})
        return data[0].get("details", []) if data else []

    # -------------------------------------------------------------- private
    def account_config(self) -> dict:
        return self.request("GET", "/api/v5/account/config", private=True)[0]

    def balance(self, ccy: str = "USDT") -> dict:
        return self.request("GET", "/api/v5/account/balance", {"ccy": ccy}, private=True)[0]

    def positions(self, inst_id: str | None = None) -> list[dict]:
        return self.request("GET", "/api/v5/account/positions",
                            {"instType": "SWAP", "instId": inst_id}, private=True)

    def set_leverage(self, inst_id: str, lever: int, mgn_mode: str, pos_side: str | None = None) -> list:
        body = {"instId": inst_id, "lever": str(lever), "mgnMode": mgn_mode}
        if pos_side:
            body["posSide"] = pos_side
        return self.request("POST", "/api/v5/account/set-leverage", body=body, private=True)

    def place_order(self, body: dict) -> dict:
        return self.request("POST", "/api/v5/trade/order", body=body, private=True)[0]

    def get_order(self, inst_id: str, ord_id: str | None = None, cl_ord_id: str | None = None) -> dict:
        return self.request("GET", "/api/v5/trade/order",
                            {"instId": inst_id, "ordId": ord_id, "clOrdId": cl_ord_id}, private=True)[0]

    def pending_orders(self, inst_id: str | None = None) -> list[dict]:
        return self.request("GET", "/api/v5/trade/orders-pending",
                            {"instType": "SWAP", "instId": inst_id}, private=True)

    def cancel_order(self, inst_id: str, ord_id: str) -> dict:
        return self.request("POST", "/api/v5/trade/cancel-order",
                            body={"instId": inst_id, "ordId": ord_id}, private=True)[0]

    def pending_algos(self, inst_id: str, ord_type: str) -> list[dict]:
        return self.request("GET", "/api/v5/trade/orders-algo-pending",
                            {"ordType": ord_type, "instType": "SWAP", "instId": inst_id}, private=True)

    def positions_history(self, inst_id: str, limit: int = 20) -> list[dict]:
        return self.request("GET", "/api/v5/account/positions-history",
                            {"instType": "SWAP", "instId": inst_id, "limit": str(limit)}, private=True)
