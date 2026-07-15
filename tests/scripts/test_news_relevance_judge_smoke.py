# tests/scripts/test_news_relevance_judge_smoke.py
"""ROB-889: news-relevance judgment plumbing helper — no LLM, safe HTTP glue."""

from __future__ import annotations

import json
from typing import Any

import pytest

from scripts import news_relevance_judge_smoke as smoke


def _valid_judgment(**over: Any) -> dict[str, Any]:
    base = {
        "article_id": 123,
        "market": "kr",
        "symbol": "035420",
        "relationship": "direct",
        "relevance": "high",
        "price_relevance": "catalyst",
        "score": 0.9,
        "reason": "급락 원인 직접 보도",
        "judged_by": "session",
    }
    base.update(over)
    return base


# --- token / secret safety -------------------------------------------------


def test_require_ingest_token_returns_when_set() -> None:
    assert smoke.require_ingest_token({"NEWS_RELEVANCE_INGEST_TOKEN": "s3cret"}) == (
        "s3cret"
    )


def test_require_ingest_token_raises_without_leaking_value() -> None:
    with pytest.raises(smoke.SmokeRejected) as exc:
        smoke.require_ingest_token({"NEWS_RELEVANCE_INGEST_TOKEN": ""})
    msg = str(exc.value)
    assert "NEWS_RELEVANCE_INGEST_TOKEN" in msg  # names the key
    assert "value never printed" in msg


def test_require_ingest_token_missing_key() -> None:
    with pytest.raises(smoke.SmokeRejected):
        smoke.require_ingest_token({})


def test_build_auth_headers_uses_default_header_name() -> None:
    headers = smoke.build_auth_headers("tok", env={})
    assert headers == {"X-News-Relevance-Ingest-Token": "tok"}


def test_build_auth_headers_respects_header_override() -> None:
    headers = smoke.build_auth_headers(
        "tok", env={"NEWS_RELEVANCE_INGEST_TOKEN_HEADER": "X-Custom"}
    )
    assert headers == {"X-Custom": "tok"}


# --- host resolution -------------------------------------------------------


def test_resolve_host_prefers_arg_then_env_then_default() -> None:
    assert smoke.resolve_host("https://h/", env={}) == "https://h"
    assert smoke.resolve_host(None, env={"NEWS_RELEVANCE_SMOKE_HOST": "https://e"}) == (
        "https://e"
    )
    assert smoke.resolve_host(None, env={}) == "http://localhost:8000"


# --- payload validation (offline, mirrors server 422) ----------------------


def test_validate_accepts_wrapped_and_bare() -> None:
    wrapped = smoke.validate_judgments_payload({"judgments": [_valid_judgment()]})
    bare = smoke.validate_judgments_payload([_valid_judgment()])
    assert len(wrapped.judgments) == 1
    assert len(bare.judgments) == 1


def test_validate_rejects_bad_enum() -> None:
    with pytest.raises(smoke.SmokeRejected) as exc:
        smoke.validate_judgments_payload(
            [_valid_judgment(relationship="totally-made-up")]
        )
    assert "invalid judgments payload" in str(exc.value)


def test_validate_rejects_empty_batch() -> None:
    with pytest.raises(smoke.SmokeRejected):
        smoke.validate_judgments_payload([])


def test_validate_rejects_non_container() -> None:
    with pytest.raises(smoke.SmokeRejected):
        smoke.validate_judgments_payload("nope")


def test_summarize_applies_server_exclusion_rule() -> None:
    request = smoke.validate_judgments_payload(
        [
            _valid_judgment(article_id=1),  # confirm
            _valid_judgment(article_id=2, relationship="unrelated"),  # exclude
            _valid_judgment(article_id=3, relevance="low"),  # exclude
        ]
    )
    assert smoke.summarize_judgments(request) == {
        "judgments": 3,
        "would_exclude": 2,
        "would_confirm": 1,
    }


# --- file loading ----------------------------------------------------------


def test_load_judgments_file_missing(tmp_path) -> None:
    with pytest.raises(smoke.SmokeRejected):
        smoke.load_judgments_file(str(tmp_path / "nope.json"))


def test_load_judgments_file_bad_json(tmp_path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(smoke.SmokeRejected):
        smoke.load_judgments_file(str(p))


def test_load_judgments_file_valid(tmp_path) -> None:
    p = tmp_path / "j.json"
    p.write_text(json.dumps({"judgments": [_valid_judgment()]}), encoding="utf-8")
    request = smoke.load_judgments_file(str(p))
    assert request.judgments[0].symbol == "035420"


# --- HTTP glue (fake client, no network) -----------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload

    @property
    def text(self) -> str:
        return json.dumps(self._payload)


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.get_calls: list[dict[str, Any]] = []
        self.post_calls: list[dict[str, Any]] = []

    async def get(self, url, params=None, headers=None) -> _FakeResponse:
        self.get_calls.append({"url": url, "params": params, "headers": headers})
        return self._response

    async def post(self, url, json=None, headers=None) -> _FakeResponse:
        self.post_calls.append({"url": url, "json": json, "headers": headers})
        return self._response


@pytest.mark.asyncio
async def test_fetch_pending_sends_token_and_params() -> None:
    client = _FakeClient(
        _FakeResponse(200, {"market": "kr", "count": 0, "pending": []})
    )
    out = await smoke.fetch_pending(
        host="https://h",
        token="tok",
        market="kr",
        limit=25,
        symbol="035420",
        client=client,
        env={},
    )
    assert out == {
        "http_status": 200,
        "body": {"market": "kr", "count": 0, "pending": []},
    }
    call = client.get_calls[0]
    assert call["url"] == "https://h/trading/api/news-relevance/pending"
    assert call["params"] == {"market": "kr", "limit": 25, "symbol": "035420"}
    assert call["headers"] == {"X-News-Relevance-Ingest-Token": "tok"}


@pytest.mark.asyncio
async def test_fetch_pending_omits_symbol_when_absent() -> None:
    client = _FakeClient(_FakeResponse(200, {"pending": []}))
    await smoke.fetch_pending(
        host="https://h", token="tok", market="kr", limit=50, client=client, env={}
    )
    assert client.get_calls[0]["params"] == {"market": "kr", "limit": 50}


@pytest.mark.asyncio
async def test_submit_judgments_posts_body_and_token() -> None:
    request = smoke.validate_judgments_payload([_valid_judgment()])
    client = _FakeClient(
        _FakeResponse(200, {"applied": [{"status": "confirmed"}], "errors": []})
    )
    out = await smoke.submit_judgments(
        host="https://h", token="tok", request=request, client=client, env={}
    )
    assert out["http_status"] == 200
    call = client.post_calls[0]
    assert call["url"] == "https://h/trading/api/news-relevance/ingest/bulk"
    assert call["headers"] == {"X-News-Relevance-Ingest-Token": "tok"}
    assert call["json"]["judgments"][0]["symbol"] == "035420"
    # no server-derived 'status' is ever sent
    assert "status" not in call["json"]["judgments"][0]


@pytest.mark.asyncio
async def test_submit_surfaces_non_2xx_status() -> None:
    request = smoke.validate_judgments_payload([_valid_judgment()])
    client = _FakeClient(_FakeResponse(403, {"detail": "token not configured"}))
    out = await smoke.submit_judgments(
        host="https://h", token="tok", request=request, client=client, env={}
    )
    assert out["http_status"] == 403


# --- CLI gating ------------------------------------------------------------


def test_main_submit_dry_run_without_confirm_does_not_require_token(
    tmp_path, capsys
) -> None:
    p = tmp_path / "j.json"
    p.write_text(json.dumps([_valid_judgment()]), encoding="utf-8")
    # No token in env, but dry-run submit must not need it and must not POST.
    rc = smoke.main(["--mode", "submit", "--file", str(p)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "dry_run"
    assert out["would_confirm"] == 1


def test_main_validate_requires_file() -> None:
    with pytest.raises(smoke.SmokeRejected):
        smoke.main(["--mode", "validate"])


def test_main_validate_ok(tmp_path, capsys) -> None:
    p = tmp_path / "j.json"
    p.write_text(json.dumps([_valid_judgment()]), encoding="utf-8")
    rc = smoke.main(["--mode", "validate", "--file", str(p)])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["status"] == "valid"
