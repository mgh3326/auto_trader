# tests/services/investment_snapshots/test_append_only.py
import inspect

from app.services.investment_snapshots import repository as repo_mod

_FORBIDDEN_PREFIXES = ("update_", "delete_", "remove_", "mutate_", "patch_")


def test_repository_surface_has_no_mutation_methods():
    cls = repo_mod.InvestmentSnapshotsRepository
    public_methods = [
        name
        for name, _ in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]
    forbidden = [m for m in public_methods if m.startswith(_FORBIDDEN_PREFIXES)]
    assert forbidden == [], (
        f"Append-only invariant violated: {forbidden}. "
        "Snapshot artifacts must be immutable; status transitions live on "
        "the run row only and require a separate write path with reviewer "
        "sign-off."
    )


def test_repository_surface_only_inserts_and_links_and_reads():
    cls = repo_mod.InvestmentSnapshotsRepository
    public_methods = sorted(
        name
        for name, _ in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    )
    # Lock the surface — adding a new method requires this test to be updated
    # which forces reviewer awareness.
    # Phase 2 (rob-269-phase2-mcp-api) added 5 SELECT-only read methods to
    # support the MCP/API surface. None of them mutate; the mutation prefix
    # guard above still rejects update_/delete_/etc.
    # ROB-275 added get_bundle_item_with_snapshot — a SELECT-only membership
    # check used by the report-centric evidence viewer.
    # ROB-380 added list_account_independent_bundle_snapshots — a SELECT-only
    # read used by the mock_preview reuse path. Still no mutation methods.
    assert public_methods == [
        "find_latest_bundle",
        "get_bundle_by_uuid",
        "get_bundle_item_with_snapshot",
        "get_run_by_uuid",
        "get_snapshot_by_uuid",
        "insert_bundle",
        "insert_run",
        "insert_snapshot",
        "link_bundle_item",
        "list_account_independent_bundle_snapshots",
        "list_bundle_items_with_snapshots",
        "list_bundles",
        "list_snapshots",
    ]
