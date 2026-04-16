from __future__ import annotations

import json
import subprocess
from pathlib import Path

WORKFLOW_PATH = Path("n8n/data/export-check/tc-briefing-discord.json")
COMPOSE_PATH = Path("docker-compose.n8n.yml")


def _run_build_embeds_node(env: dict[str, str], payload: dict) -> dict:
    workflow = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    node = next(node for node in workflow["nodes"] if node["id"] == "tc-build-embeds")
    js_code = node["parameters"]["jsCode"]

    runner = """
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const fn = new Function('$env', '$input', input.jsCode);
const result = fn(input.env, { first: () => ({ json: input.payload }) });
process.stdout.write(JSON.stringify(result[0].json));
"""
    completed = subprocess.run(
        ["node", "-e", runner],
        input=json.dumps({"env": env, "payload": payload, "jsCode": js_code}),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


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
