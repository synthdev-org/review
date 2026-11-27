"""adapter.storage.unit_of_work — the one place the "run against the
caller's already-open session, or open a fresh session/transaction of our
own" choice is made.

Both ``adapter/storage/queue.py``'s ``JobQueueRepository.enqueue`` and
``adapter/storage/tenancy.py``'s ``PostgresTenancyStore._run`` need exactly
this branch (an optional caller-supplied session lets
``apps/webhook_api/webhook_routes.py`` stage a queue insert and a tenancy
resolution on the *same* transaction as its own other writes; omitting it
keeps every existing, session-less call site committing its own independent
transaction exactly as before). Before this module existed the two adapters
each hand-rolled the same branch independently — this factors it out so a
future change to the join-or-open semantics only has to be made once.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def run_in_session[T](
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession | None,
    fn: Callable[[AsyncSession], Awaitable[T]],
) -> T:
    """Runs ``fn`` against ``session`` if given (an already-open,
    caller-owned session/transaction — ``fn`` must not commit or begin a
    transaction of its own on it), or opens and commits a fresh
    session/transaction from ``session_factory`` otherwise.
    """
    if session is not None:
        return await fn(session)

    async with session_factory() as opened_session, opened_session.begin():
        return await fn(opened_session)
