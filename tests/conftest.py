import json
from functools import partial
from pathlib import Path

import httpx
import pytest

import omini_omada.collect as collect_module
from omini_omada.client import Client

FIXTURES = Path(__file__).parent / "fixtures"
OMADAC_ID = "de382a0e78f4deb681f3128c3e75dbd1"
SITE = "640effd1b3f2ae5b912275ec"


class FakeOmada:
    """Answers like an Omada Software Controller 5.15 with the Open API, from the
    JSON files in fixtures/ (shaped like the examples of the online reference:
    every answer is {"errorCode", "msg", "result"})."""

    def __init__(self):
        self.routes = {}
        for f in FIXTURES.glob("*.json"):
            rel = f.stem.replace("__", "/")
            path = "/" + rel if rel.startswith("api/") else f"/openapi/v1/{OMADAC_ID}/{rel}"
            self.routes[path] = json.loads(f.read_text())
        self.client = ("client-id", "client-secret")
        self.tokens: set[str] = set()
        self.issued = 0
        self.forbidden: set[str] = set()  # path prefixes answered with -1005
        self.calls: list[str] = []
        self.requests: list[tuple[str, str]] = []
        self.token_requests = 0

    def expire_tokens(self):
        self.tokens.clear()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        self.requests.append((request.method, path))
        if path == "/api/info":
            return httpx.Response(200, json=self.routes[path])
        if path == "/openapi/authorize/token":
            return self.authorize(request)
        auth = request.headers.get("authorization", "")
        if not auth.startswith("AccessToken=") or auth[len("AccessToken=") :] not in self.tokens:
            # Documented general error code for an expired token.
            return httpx.Response(
                200, json={"errorCode": -44112, "msg": "The access token has expired."}
            )
        rel = path.removeprefix(f"/openapi/v1/{OMADAC_ID}/")
        if any(rel.startswith(p) for p in self.forbidden):
            return httpx.Response(200, json={"errorCode": -1005, "msg": "Operation forbidden."})
        if path not in self.routes:
            return httpx.Response(404, json={"errorCode": -1, "msg": "Not found"})
        body = json.loads(json.dumps(self.routes[path]))
        result = body.get("result")
        if isinstance(result, dict) and "data" in result and "page" in request.url.params:
            page = int(request.url.params["page"])
            size = int(request.url.params["pageSize"])
            assert 1 <= size <= 1000, "pageSize must be within 1-1000"
            result["data"] = result["data"][(page - 1) * size : page * size]
            result["currentPage"], result["currentSize"] = page, size
        return httpx.Response(200, json=body)

    def authorize(self, request: httpx.Request) -> httpx.Response:
        self.token_requests += 1
        assert request.method == "POST"
        assert request.url.params.get("grant_type") == "client_credentials"
        body = json.loads(request.content)
        if body.get("omadacId") != OMADAC_ID:
            return httpx.Response(
                200, json={"errorCode": -44116, "msg": "Open API authorized failed"}
            )
        if (body.get("client_id"), body.get("client_secret")) != self.client:
            return httpx.Response(
                200, json={"errorCode": -44106, "msg": "The client id or client secret is invalid"}
            )
        self.issued += 1
        token = f"AT-{self.issued:032d}"
        self.tokens.add(token)
        return httpx.Response(
            200,
            json={
                "errorCode": 0,
                "msg": "Open API Get Access Token successfully.",
                "result": {
                    "accessToken": token,
                    "tokenType": "bearer",
                    "expiresIn": 7200,
                    "refreshToken": "RT-HqvaDuSxEqayM75U2ukTRnBl6f6fiRAc",
                },
            },
        )


@pytest.fixture
def omada(monkeypatch):
    fake = FakeOmada()
    monkeypatch.setattr(
        collect_module,
        "Client",
        partial(Client, transport=httpx.MockTransport(fake.handler), min_interval=0),
    )
    return fake


@pytest.fixture
def cfg(tmp_path):
    from omini_sdk import Config

    return Config(
        {
            "url": "https://192.168.0.5:8043",
            "client_id": "client-id",
            "client_secret": "client-secret",
        },
        state_dir=tmp_path / "state",
    )
