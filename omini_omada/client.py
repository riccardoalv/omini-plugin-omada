"""Minimal client for the Omada Open API (client credentials mode).

Access guide: the "Open API Access Guide" on the home page of the online
reference (https://use1-omada-northbound.tplinkcloud.com/doc.html), sections
2.2 and 2.3. Every answer is ``{"errorCode": 0, "msg": "...", "result": ...}``;
an error code other than 0 is a failure even with HTTP 200.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from omini_sdk import PluginError, log

# Error codes from section 1.2 of the access guide.
INVALID_CLIENT = -44106
TOKEN_EXPIRED = -44112
TOKEN_INVALID = -44113
FORBIDDEN = (-1005, -1505)

PAGE_SIZE = 1000  # the largest page the API accepts


class Unavailable(Exception):
    """An endpoint that could not be read: missing on this controller version,
    forbidden to the application's role, or failing. Optional data is skipped."""


class Client:
    def __init__(
        self,
        url: str,
        client_id: str,
        client_secret: str,
        verify_tls: bool = False,
        state_dir: Path | None = None,
        timeout: float = 10,
        transport: httpx.BaseTransport | None = None,
        min_interval: float = 0.12,
        sleep: Callable[[float], None] = time.sleep,
    ):
        url = url.strip().rstrip("/")
        if not url.startswith(("https://", "http://")):
            url = "https://" + url
        self.base = url
        self.client_id = client_id
        self.client_secret = client_secret
        self.state_dir = state_dir
        # Omada controllers allow about 10 requests per second (access guide,
        # "API call budget"): requests are spaced a little above that.
        self.min_interval = min_interval
        self.sleep = sleep
        self._last = 0.0
        self.http = httpx.Client(
            base_url=url,
            verify=verify_tls,
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "omini-plugin-omada"},
            transport=transport,
        )
        self._info: dict[str, Any] | None = None
        self._token: str | None = None
        self._cache_key = hashlib.sha256(
            f"{url}\n{client_id}\n{client_secret}".encode()
        ).hexdigest()

    def close(self) -> None:
        self.http.close()

    # -- controller -------------------------------------------------------

    def info(self) -> dict[str, Any]:
        """``GET /api/info``: the controller's version and Omada ID (omadacId).
        Needs no login; the controller's own web interface reads it first."""
        if self._info is None:
            r = self._send("GET", "/api/info")
            body = self._json(r)
            result = body.get("result") if isinstance(body, dict) else None
            if r.status_code >= 400 or not isinstance(result, dict) or not result.get("omadacId"):
                raise PluginError(
                    f"{self.base} did not report an Omada ID: is it an Omada controller "
                    "(Software Controller or OC200/OC300, version 5.9 or later)?"
                )
            self._info = result
        return self._info

    @property
    def omadac_id(self) -> str:
        return str(self.info()["omadacId"])

    # -- access token -----------------------------------------------------

    @property
    def _token_file(self) -> Path | None:
        return self.state_dir / "token.json" if self.state_dir else None

    def _cached_token(self) -> str | None:
        f = self._token_file
        if not f or not f.exists():
            return None
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            return None
        if (
            data.get("key") == self._cache_key
            and data.get("omadacId") == self.omadac_id
            and float(data.get("expiresAt", 0)) > time.time() + 60
        ):
            return data.get("accessToken")
        return None

    def _save_token(self, token: str, expires_in: float) -> None:
        f = self._token_file
        if not f:
            return
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as out:
                json.dump(
                    {
                        "key": self._cache_key,  # hash of address + credentials
                        "omadacId": self.omadac_id,
                        "accessToken": token,
                        "expiresAt": time.time() + expires_in,
                    },
                    out,
                )
        except OSError as e:
            log.debug("could not keep the access token: %s", e)

    def _forget_token(self) -> None:
        self._token = None
        f = self._token_file
        if f and f.exists():
            with contextlib.suppress(OSError):
                f.unlink()

    def authorize(self) -> str:
        """A new access token (client credentials mode, access guide 2.3.1).
        Valid for 2 hours. A new one is requested when it expires instead of
        using the refresh token: the refresh request carries the secret in the
        URL, and client mode can always ask again."""
        r = self._send(
            "POST",
            "/openapi/authorize/token",
            params={"grant_type": "client_credentials"},
            json={
                "omadacId": self.omadac_id,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        body = self._json(r)
        code = body.get("errorCode") if isinstance(body, dict) else None
        if code == INVALID_CLIENT or r.status_code == 401:
            raise PluginError("the Omada controller rejected the client ID or secret")
        result = body.get("result") if isinstance(body, dict) else None
        if code != 0 or not isinstance(result, dict) or not result.get("accessToken"):
            msg = body.get("msg") if isinstance(body, dict) else None
            raise PluginError(
                f"the Omada controller refused the Open API authorization ({code}: {msg})"
            )
        token = str(result["accessToken"])
        self._save_token(token, float(result.get("expiresIn") or 7200))
        self._token = token
        return token

    def token(self) -> str:
        if not self._token:
            self._token = self._cached_token() or self.authorize()
        return self._token

    # -- API --------------------------------------------------------------

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GETs ``/openapi/v1/{omadacId}/<path>`` and returns its ``result``."""
        full = f"/openapi/v1/{self.omadac_id}/{path.lstrip('/')}"
        for attempt in range(2):
            r = self._send(
                "GET", full, params=params, headers={"Authorization": f"AccessToken={self.token()}"}
            )
            if r.status_code == 401 and attempt == 0:
                self._forget_token()
                continue
            if r.status_code == 401:
                raise PluginError("the Omada controller rejected the access token")
            if r.status_code in (403, 404):
                raise Unavailable(f"{path}: HTTP {r.status_code}")
            if r.status_code >= 400:
                raise Unavailable(f"{path}: HTTP {r.status_code}")
            body = self._json(r)
            if not isinstance(body, dict):
                raise Unavailable(f"{path}: unexpected answer")
            code = body.get("errorCode")
            if code in (TOKEN_EXPIRED, TOKEN_INVALID) and attempt == 0:
                self._forget_token()
                continue
            if code == 0:
                return body.get("result")
            raise Unavailable(f"{path}: error {code} ({body.get('msg')})")
        raise PluginError("the Omada controller keeps refusing new access tokens")

    def pages(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Every row of a paged list (``result.data`` with ``totalRows``)."""
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            result = self.get(path, {**(params or {}), "page": page, "pageSize": PAGE_SIZE})
            data = (result or {}).get("data") if isinstance(result, dict) else None
            if not isinstance(data, list):
                return rows
            rows.extend(d for d in data if isinstance(d, dict))
            total = (result or {}).get("totalRows")
            if not data or not isinstance(total, int) or len(rows) >= total or page >= 50:
                return rows
            page += 1

    # -- transport --------------------------------------------------------

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(3):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self.sleep(wait)
            try:
                r = self.http.request(method, path, **kwargs)
            except httpx.ConnectError as e:
                raise PluginError(f"cannot connect to {self.base}: {e}") from e
            except httpx.TimeoutException as e:
                raise PluginError(f"{self.base} did not answer in time") from e
            except httpx.HTTPError as e:
                raise PluginError(f"request to {self.base} failed: {e}") from e
            finally:
                self._last = time.monotonic()
            if r.status_code == 429 and attempt < 2:  # API rate limit exceeded
                self.sleep(1.0)
                continue
            return r
        return r

    def _json(self, r: httpx.Response) -> Any:
        try:
            return r.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise PluginError(
                f"{self.base} did not answer with JSON: is it the Omada controller's address?"
            ) from e
