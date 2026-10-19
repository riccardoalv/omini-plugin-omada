"""Client for the Omada Open API (client credentials mode), on the SDK's HTTP
client: timeouts, retries (a refused connection, HTTP 429 or 503) and the
SDK's errors.

Access guide: the "Open API Access Guide" on the home page of the online
reference (https://use1-omada-northbound.tplinkcloud.com/doc.html), sections
2.2 and 2.3. Every answer is ``{"errorCode": 0, "msg": "...", "result": ...}``;
an error code other than 0 is a failure even with HTTP 200.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from omini_sdk import PluginError
from omini_sdk.http import Client as HttpClient
from omini_sdk.http import (
    Forbidden,
    HttpError,
    NotFound,
    Session,
    Unauthorized,
    Unavailable,
)

__all__ = ["ApiError", "Client", "Forbidden", "HttpError", "NotFound", "Unavailable"]

# Error codes from section 1.2 of the access guide.
INVALID_CLIENT = -44106
TOKEN_EXPIRED = -44112
TOKEN_INVALID = -44113
FORBIDDEN = (-1005, -1505)
UNSUPPORTED_PATH = -1600

PAGE_SIZE = 1000  # the largest page the API accepts


class ApiError(HttpError):
    """The controller refused a change with an error code (``code``)."""

    def __init__(self, message: str, *, code: Any = None, **kw: Any):
        super().__init__(message, **kw)
        self.code = code


class Client(HttpClient):
    def __init__(
        self,
        url: str,
        client_id: str,
        client_secret: str,
        verify_tls: bool = False,
        state_dir: Path | None = None,
        timeout: float = 15,
        transport: httpx.BaseTransport | None = None,
        min_interval: float = 0.12,
        sleep: Callable[[float], None] | None = None,
    ):
        super().__init__(
            url,
            name="Omada controller",
            verify_tls=verify_tls,
            timeout=timeout,
            user_agent="omini-plugin-omada",
            unauthorized="the Omada controller rejected the access token",
            transport=transport,
            sleep=sleep,
        )
        self.client_id = client_id
        self.client_secret = client_secret
        # Omada controllers allow about 10 requests per second (access guide,
        # "API call budget"): requests are spaced a little above that. A 429
        # is asked again by the SDK's client.
        self.min_interval = min_interval
        self._last = 0.0
        self._info: dict[str, Any] | None = None
        self._token: str | None = None
        # The access token, kept in token.json (tied to a hash of the address
        # and credentials; the secret is never stored).
        self.session = Session(state_dir, self.base, client_id, client_secret, name="token")

    # -- controller -------------------------------------------------------

    def info(self) -> dict[str, Any]:
        """``GET /api/info``: the controller's version and Omada ID (omadacId).
        Needs no login; the controller's own web interface reads it first."""
        if self._info is None:
            r = self.send("GET", "/api/info")
            body = self.json(r)
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

    def _cached_token(self) -> str | None:
        data = self.session.load()
        try:
            fresh = float(data.get("expiresAt", 0)) > time.time() + 60
        except (TypeError, ValueError):
            return None
        if data.get("omadacId") == self.omadac_id and fresh and data.get("accessToken"):
            return str(data["accessToken"])
        return None

    def _save_token(self, token: str, expires_in: float) -> None:
        self.session.save(
            {
                "omadacId": self.omadac_id,
                "accessToken": token,
                "expiresAt": time.time() + expires_in,
            }
        )

    def _forget_token(self) -> None:
        self._token = None
        self.session.clear()

    def authorize(self) -> str:
        """A new access token (client credentials mode, access guide 2.3.1).
        Valid for 2 hours. A new one is requested when it expires instead of
        using the refresh token: the refresh request carries the secret in the
        URL, and client mode can always ask again."""
        path = "/openapi/authorize/token"
        r = self.send(
            "POST",
            path,
            params={"grant_type": "client_credentials"},
            json={
                "omadacId": self.omadac_id,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        body = self.json(r, path)
        code = body.get("errorCode") if isinstance(body, dict) else None
        if code == INVALID_CLIENT or r.status_code == 401:
            raise Unauthorized(
                "the Omada controller rejected the client ID or secret",
                status=r.status_code,
                method="POST",
                resource=path,
            )
        if r.status_code >= 400 and code is None:
            self.check(r, "POST", path)
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

    def get(self, path: str, params: dict[str, Any] | None = None, api: str = "v1") -> Any:  # type: ignore[override]
        """GETs ``/openapi/{api}/{omadacId}/<path>`` and returns its ``result``
        (``api`` is v1, or v2 for the endpoints that have a newer version)."""
        return self._call("GET", path, params=params, api=api)

    def put(self, path: str, body: dict[str, Any]) -> Any:  # type: ignore[override]
        """PUTs a configuration change (only ``apply`` calls it, for the changes
        the manifest declares). Whether it took effect is checked by reading
        again, never assumed from the answer."""
        return self._call("PUT", path, json=body)

    def post(self, path: str, body: dict[str, Any], api: str = "v1") -> Any:  # type: ignore[override]
        """POSTs something new (a change, as put)."""
        return self._call("POST", path, json=body, api=api)

    def patch(self, path: str, body: dict[str, Any], api: str = "v1") -> Any:  # type: ignore[override]
        """PATCHes a configuration change (as put)."""
        return self._call("PATCH", path, json=body, api=api)

    def delete(self, path: str) -> Any:  # type: ignore[override]
        """DELETEs something (a change, as put)."""
        return self._call("DELETE", path)

    def _call(self, method: str, path: str, api: str = "v1", **kwargs: Any) -> Any:
        full = f"/openapi/{api}/{self.omadac_id}/{path.lstrip('/')}"
        where = {"method": method, "resource": path}
        for attempt in range(2):
            r = self.send(
                method, full, headers={"Authorization": f"AccessToken={self.token()}"}, **kwargs
            )
            if r.status_code == 401 and attempt == 0:
                self._forget_token()
                continue
            # 401 Unauthorized, 403 Forbidden, 404 NotFound, 429/5xx
            # Unavailable, another 4xx HttpError.
            self.check(r, method, path)
            body = self.json(r, path)
            if not isinstance(body, dict):
                raise Unavailable(f"{path}: unexpected answer", status=r.status_code, **where)
            code = body.get("errorCode")
            if code in (TOKEN_EXPIRED, TOKEN_INVALID) and attempt == 0:
                self._forget_token()
                continue
            if code == 0:
                return body.get("result")
            msg = f"{path}: error {code} ({body.get('msg')})"
            if code in FORBIDDEN:
                raise Forbidden(msg, **where)
            if code == UNSUPPORTED_PATH:
                raise NotFound(msg, status=r.status_code, **where)
            if method == "GET":
                # A read the controller fails to answer: optional data is skipped.
                raise Unavailable(msg, status=r.status_code, **where)
            raise ApiError(msg, code=code, status=r.status_code, **where)
        raise PluginError("the Omada controller keeps refusing new access tokens")

    def pages(
        self, path: str, params: dict[str, Any] | None = None, api: str = "v1"
    ) -> list[dict[str, Any]]:
        """Every row of a paged list (``result.data`` with ``totalRows``)."""
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            result = self.get(path, {**(params or {}), "page": page, "pageSize": PAGE_SIZE}, api)
            data = (result or {}).get("data") if isinstance(result, dict) else None
            if not isinstance(data, list):
                return rows
            rows.extend(d for d in data if isinstance(d, dict))
            total = (result or {}).get("totalRows")
            if not data or not isinstance(total, int) or len(rows) >= total or page >= 50:
                return rows
            page += 1

    # -- transport --------------------------------------------------------

    def send(self, method: str, path: str, **kw: Any) -> httpx.Response:
        """Sends a request (with the SDK's retries), spaced from the last one."""
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            (self.sleep or time.sleep)(wait)
        try:
            return super().send(method, path, **kw)
        finally:
            self._last = time.monotonic()
