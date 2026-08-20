"""Mutation counter for ROB-1297 AC3.

Counts SQLAlchemy unit-of-work writes (new/dirty/deleted) plus Core
INSERT/UPDATE/DELETE statements executed on the session. SELECT does not
increment. The digest path must report total == 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Delete, Insert, Update


@dataclass
class MutationCounter:
    inserts: int = 0
    updates: int = 0
    deletes: int = 0
    core_dml: int = 0
    _hooks: list[tuple[object, str, object]] = field(default_factory=list, repr=False)

    @property
    def total(self) -> int:
        return self.inserts + self.updates + self.deletes + self.core_dml

    def detach(self) -> None:
        for target, name, listener in self._hooks:
            if event.contains(target, name, listener):
                event.remove(target, name, listener)
        self._hooks.clear()


def attach_mutation_counter(session: AsyncSession) -> MutationCounter:
    """Listen on ``session.sync_session`` until ``detach`` is called."""
    counter = MutationCounter()
    sync_session = session.sync_session

    def _before_flush(sess, _flush_context, _instances) -> None:  # noqa: ANN001
        counter.inserts += len(sess.new)
        counter.updates += len(sess.dirty)
        counter.deletes += len(sess.deleted)

    def _do_orm_execute(orm_execute_state) -> None:  # noqa: ANN001
        statement = orm_execute_state.statement
        if isinstance(statement, (Insert, Update, Delete)):
            counter.core_dml += 1

    event.listen(sync_session, "before_flush", _before_flush)
    event.listen(sync_session, "do_orm_execute", _do_orm_execute)
    counter._hooks = [
        (sync_session, "before_flush", _before_flush),
        (sync_session, "do_orm_execute", _do_orm_execute),
    ]
    return counter
