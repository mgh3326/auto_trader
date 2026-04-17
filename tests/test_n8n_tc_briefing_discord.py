from __future__ import annotations

import json
import subprocess
from pathlib import Path

WORKFLOW_PATH = Path("n8n/workflows/tc-briefing-discord.json")
COMPOSE_PATH = Path("docker-compose.n8n.yml")


def _run_build_embeds_items(env: dict[str, str], payload: dict) -> list[dict]:
    workflow = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    node = next(node for node in workflow["nodes"] if node["id"] == "tc-build-embeds")
    js_code = node["parameters"]["jsCode"]

    runner = """
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const fn = new Function('$env', '$input', input.jsCode);
const result = fn(input.env, { first: () => ({ json: input.payload }) });
process.stdout.write(JSON.stringify(result.map(item => item.json)));
"""
    completed = subprocess.run(
        ["node", "-e", runner],
        input=json.dumps({"env": env, "payload": payload, "jsCode": js_code}),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _run_build_embeds_node(env: dict[str, str], payload: dict) -> dict:
    return _run_build_embeds_items(env, payload)[0]


def test_tc_briefing_buttons_use_paperclip_base_url_with_identifier() -> None:
    result = _run_build_embeds_node(
        {
            "DISCORD_TC_BRIEFING_BOT_TOKEN": "bot-token",
            "DISCORD_TC_BRIEFING_CHANNEL_ID": "channel-1",
            "PAPERCLIP_UI_BASE_URL": "http://raspberrypi:3100",
        },
        {
            "briefing": [
                {
                    "symbol": "SK하이닉스",
                    "action": "sell",
                    "paperclip_issue_identifier": "ROB-99",
                    "paperclip_issue_url": "https://example.com/ROB/issues/ROB-99",
                }
            ],
        },
    )

    button = result["payload"]["components"][0]["components"][0]
    assert button["url"] == "http://raspberrypi:3100/ROB/issues/ROB-99"


def test_tc_briefing_buttons_extract_identifier_from_url_when_identifier_missing() -> (
    None
):
    result = _run_build_embeds_node(
        {
            "DISCORD_TC_BRIEFING_BOT_TOKEN": "bot-token",
            "DISCORD_TC_BRIEFING_CHANNEL_ID": "channel-1",
            "PAPERCLIP_UI_BASE_URL": "http://raspberrypi:3100/",
        },
        {
            "briefing": [
                {
                    "symbol": "삼성전자",
                    "action": "hold",
                    "paperclip_issue_url": "https://external.example/ROB/issues/ROB-100",
                }
            ],
        },
    )

    button = result["payload"]["components"][0]["components"][0]
    assert button["url"] == "http://raspberrypi:3100/ROB/issues/ROB-100"


def test_tc_briefing_buttons_keep_original_url_without_paperclip_base_url() -> None:
    original_url = "https://external.example/ROB/issues/ROB-101"
    result = _run_build_embeds_node(
        {
            "DISCORD_TC_BRIEFING_BOT_TOKEN": "bot-token",
            "DISCORD_TC_BRIEFING_CHANNEL_ID": "channel-1",
            "PAPERCLIP_UI_BASE_URL": "",
        },
        {
            "briefing": [
                {
                    "symbol": "LG화학",
                    "action": "buy",
                    "paperclip_issue_url": original_url,
                }
            ],
        },
    )

    button = result["payload"]["components"][0]["components"][0]
    assert button["url"] == original_url


def test_n8n_compose_passes_paperclip_ui_base_url() -> None:
    compose = COMPOSE_PATH.read_text(encoding="utf-8")

    assert "PAPERCLIP_UI_BASE_URL=${PAPERCLIP_UI_BASE_URL:-}" in compose


def test_tc_briefing_phase_mode_returns_preliminary_then_cio_pending() -> None:
    items = _run_build_embeds_items(
        {
            "DISCORD_TC_BRIEFING_BOT_TOKEN": "bot-token",
            "DISCORD_TC_BRIEFING_CHANNEL_ID": "channel-1",
        },
        {
            "phase": "tc_preliminary",
            "text": "자금 현황\n경로 A·B 병행 가능",
            "gate_results": {
                "G1": {"status": "pass", "detail": "OK"},
                "G2": {
                    "passed": False,
                    "blocking_reason": "runway recovery requires cash",
                },
            },
            "generated_at": "2026-04-17T09:00:00+09:00",
        },
    )

    assert [item["phase"] for item in items] == ["tc_preliminary", "cio_pending"]
    assert (
        items[0]["payload"]["embeds"][0]["title"]
        == "📊 TC Preliminary — 자금 현황 재계산"
    )
    assert (
        items[1]["payload"]["embeds"][0]["title"]
        == "🎯 CIO Pending Decision — Gate 판정 결과"
    )


def test_tc_briefing_phase_content_separates_recommendation_and_gate_sections() -> None:
    items = _run_build_embeds_items(
        {
            "DISCORD_TC_BRIEFING_BOT_TOKEN": "bot-token",
            "DISCORD_TC_BRIEFING_CHANNEL_ID": "channel-1",
        },
        {
            "phase": "cio_pending",
            "text": "자금 현황\n경로 A·B 병행 가능",
            "gate_results": {
                "G1": {"status": "pass", "detail": "OK"},
                "G2": {
                    "passed": False,
                    "blocking_reason": "runway recovery requires cash",
                },
                "G3": {"status": "tbd", "detail": "TBD (S3)"},
                "G4": {"status": "tbd", "detail": "TBD (S4)"},
                "G5": {"status": "tbd", "detail": "TBD (S5)"},
                "G6": {"status": "tbd", "detail": "TBD (S6)"},
            },
        },
    )

    preliminary_content = items[0]["payload"]["content"]
    pending_content = items[1]["payload"]["content"]

    assert "🎯 권고" not in preliminary_content
    assert "📊 Gate 판정 결과" not in preliminary_content
    assert "📊 Gate 판정 결과" in pending_content
    for gate in ("G1", "G2", "G3", "G4", "G5", "G6"):
        assert gate in pending_content
    assert "[funding]" in pending_content
    assert "[action]" in pending_content
