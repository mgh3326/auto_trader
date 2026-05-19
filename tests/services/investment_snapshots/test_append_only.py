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
    forbidden = [
        m for m in public_methods if m.startswith(_FORBIDDEN_PREFIXES)
    ]
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
    assert public_methods == [
        "get_run_by_uuid",
        "get_snapshot_by_uuid",
        "insert_bundle",
        "insert_run",
        "insert_snapshot",
        "link_bundle_item",
    ]
