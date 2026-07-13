import json
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
SETTINGS = REPO / ".claude" / "settings.readonly.json"
TOOLING = REPO / "app" / "mcp_server" / "tooling"

# spec §6 deny-list (논리 도구명). 새 mutation 도구가 생기면 여기 + JSON에 추가해야 테스트 통과.
KNOWN_MUTATION_TOOLS = frozenset(
    {
        "place_order",
        "kis_mock_mirror_execute_report",
        "kis_mock_reconciliation_run",
        "kiwoom_mock_preview_order",
        "toss_preview_order",
        "paper_place_limit_order",
        "paper_cancel_pending_order",
        "paper_reconcile_orders",
        "binance_demo_scalping_submit_decision",
        "buy_ladder_fill_preview",
        "sell_ladder_fill_preview",
        "set_user_setting",
        "update_manual_holdings",
        "investment_report_prepare_intraday_context",
        "cancel_order",
        "modify_order",
        "kis_live_place_order",
        "kis_live_cancel_order",
        "kis_live_modify_order",
        "kis_live_reconcile_orders",
        "kis_mock_place_order",
        "kis_mock_cancel_order",
        "kis_mock_modify_order",
        "toss_place_order",
        "toss_modify_order",
        "toss_cancel_order",
        "toss_reconcile_orders",
        "alpaca_paper_submit_order",
        "alpaca_paper_automated_submit_order",
        "alpaca_paper_cancel_order",
        "kiwoom_mock_place_order",
        "kiwoom_mock_cancel_order",
        "kiwoom_mock_modify_order",
        "live_reconcile_orders",
        "investment_report_create",
        "investment_report_add_items",
        "investment_report_update",
        "investment_report_decide_item",
        "investment_report_activate_watch",
        "investment_report_set_status",
        "investment_report_generate_from_bundle",
        "investment_report_create_from_hermes_composition",
        "investment_report_prepare_bundle",
        "investment_stage_artifacts_ingest_from_hermes",
    }
)

# read/preview/handoff 도구는 mutation 이름패턴과 무관하므로 스캔에서 제외(허용 대상).
ORDER_MUTATION_RE = re.compile(
    r'name\s*=\s*["\']([a-z0-9_]*(?:place_order|cancel_order|modify_order|submit_order|reconcile_orders))["\']'
)


def _deny() -> list[str]:
    data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    return data["permissions"]["deny"]


def _denied_mcp_suffixes() -> set[str]:
    return {e.split("__")[-1] for e in _deny() if e.startswith("mcp__")}


def test_settings_file_is_valid_json_with_deny_array():
    assert isinstance(_deny(), list) and len(_deny()) > 0


def test_denies_all_known_mutation_tools():
    missing = KNOWN_MUTATION_TOOLS - _denied_mcp_suffixes()
    assert not missing, f"deny-list 누락 mutation 도구: {sorted(missing)}"


def test_denies_filesystem_and_bash_builtins():
    deny = set(_deny())
    assert {"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit"} <= deny


def test_session_context_append_is_NOT_denied():
    # 자가치유 핸드오프 적재는 의도적 허용 — deny되면 출력 경로가 막힌다.
    assert not any(e.endswith("__session_context_append") for e in _deny())


def test_analysis_bundle_read_is_allowed_but_capture_is_not() -> None:
    from app.mcp_server.tooling.analysis_readonly_registration import (
        ANALYSIS_READONLY_FORBIDDEN_TOOL_NAMES,
        ANALYSIS_READONLY_TOOL_NAMES,
    )

    assert "analysis_bundle_get" in ANALYSIS_READONLY_TOOL_NAMES
    assert "analysis_bundle_create" not in ANALYSIS_READONLY_TOOL_NAMES
    assert "analysis_bundle_create" in ANALYSIS_READONLY_FORBIDDEN_TOOL_NAMES


def test_no_new_order_mutation_tool_escapes_known_set():
    found: set[str] = set()
    for p in TOOLING.glob("*.py"):
        found |= set(ORDER_MUTATION_RE.findall(p.read_text(encoding="utf-8")))
    escaped = found - KNOWN_MUTATION_TOOLS
    assert not escaped, (
        f"새 주문 mutation 도구가 deny-list/KNOWN_MUTATION_TOOLS에 없음: {sorted(escaped)} "
        "→ .claude/settings.readonly.json deny + KNOWN_MUTATION_TOOLS 둘 다 갱신"
    )


# report/stage/watch 도구 분류 가드: 모든 investment_report*/investment_stage*/investment_watch* 도구가
# 허용 읽기 또는 deny-list에 분류되어야 한다. 새 도구 추가 시 분류를 강제한다.
ALLOWED_REPORT_READS = frozenset(
    {
        "investment_report_get",
        "investment_report_list",
        "investment_report_context_get",
        "investment_report_delta_get",
        "investment_report_get_hermes_context",
        "investment_report_prepare_intraday_context",
        "investment_watch_events_list_recent",
        "investment_watch_recommend",
    }
)

REPORT_STAGE_WATCH_RE = re.compile(
    r'name\s*=\s*["\'](investment_report[a-z_]*|investment_stage[a-z_]*|investment_watch[a-z_]*)["\']'
)


def test_every_report_stage_watch_tool_is_allowed_read_or_denied():
    found: set[str] = set()
    for p in TOOLING.glob("*.py"):
        found |= set(REPORT_STAGE_WATCH_RE.findall(p.read_text(encoding="utf-8")))
    unclassified = found - ALLOWED_REPORT_READS - _denied_mcp_suffixes()
    assert not unclassified, (
        f"미분류 report/stage/watch 도구: {sorted(unclassified)} → "
        "ALLOWED_REPORT_READS(읽기) 또는 .claude/settings.readonly.json deny(쓰기)로 분류하라"
    )
