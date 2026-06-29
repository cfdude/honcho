#!/usr/bin/env uv run python
"""Losslessly move named sessions between Honcho workspaces. See
docs/superpowers/specs/2026-06-29-cross-workspace-session-move-design.md."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src import models

_CHILD_MODELS = (
    models.Message,
    models.MessageEmbedding,
    models.Document,
    models.SessionPeer,
)


class MoveError(Exception):
    pass


@dataclass
class SessionPlan:
    source_name: str
    target_name: str
    renamed: bool
    messages: int
    embeddings: int = 0
    documents: int = 0
    peers_to_create: list[str] = field(default_factory=list)
    collections_to_create: list[tuple[str, str]] = field(default_factory=list)
    queue_rows: int = 0
    cross_boundary_premises: list[str] = field(default_factory=list)


async def _workspace_exists(session: AsyncSession, name: str) -> bool:
    r = await session.scalar(
        select(models.Workspace.name).where(models.Workspace.name == name)
    )
    return r is not None


async def _session_row(
    session: AsyncSession, ws: str, name: str
) -> models.Session | None:
    return await session.scalar(
        select(models.Session).where(
            models.Session.workspace_name == ws, models.Session.name == name
        )
    )


async def _count(session: AsyncSession, model, ws: str, name: str) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(model)
            .where(model.workspace_name == ws, model.session_name == name)
        )
        or 0
    )


async def _resolve_target_name(
    session: AsyncSession,
    target_ws: str,
    name: str,
    on_collision: str,
    rename_suffix: str,
    source_ws: str,
) -> tuple[str, bool, bool]:
    if await _session_row(session, target_ws, name) is None:
        return name, False, False
    if on_collision == "skip":
        return name, False, True
    base = name + rename_suffix.format(source=source_ws)
    candidate, n = base, 1
    while await _session_row(session, target_ws, candidate) is not None:
        n += 1
        candidate = f"{base}-{n}"
    return candidate, True, False


def _copy_row(src_obj, model, **overrides):
    """Full-column copy of an ORM row into a new instance, with overrides."""
    data = {c.name: getattr(src_obj, c.name) for c in model.__table__.columns}
    data.pop("id", None)  # let the nanoid default generate a fresh PK
    data.update(overrides)
    return model(**data)


async def _required_peers(session: AsyncSession, ws: str, name: str) -> set[str]:
    peers: set[str] = set()
    peers.update(
        await session.scalars(
            select(models.Message.peer_name.distinct()).where(
                models.Message.workspace_name == ws, models.Message.session_name == name
            )
        )
    )
    peers.update(
        await session.scalars(
            select(models.MessageEmbedding.peer_name.distinct()).where(
                models.MessageEmbedding.workspace_name == ws,
                models.MessageEmbedding.session_name == name,
            )
        )
    )
    peers.update(
        await session.scalars(
            select(models.SessionPeer.peer_name.distinct()).where(
                models.SessionPeer.workspace_name == ws,
                models.SessionPeer.session_name == name,
            )
        )
    )
    for col in (models.Document.observer, models.Document.observed):
        peers.update(
            await session.scalars(
                select(col.distinct()).where(
                    models.Document.workspace_name == ws,
                    models.Document.session_name == name,
                )
            )
        )
    peers.discard(None)
    return peers


async def _required_collections(
    session: AsyncSession, ws: str, name: str
) -> set[tuple[str, str]]:
    rows = await session.execute(
        select(models.Document.observer, models.Document.observed)
        .where(
            models.Document.workspace_name == ws,
            models.Document.session_name == name,
        )
        .distinct()
    )
    return {(o, d) for o, d in rows.all()}


async def ensure_dependencies(
    session: AsyncSession,
    source_ws: str,
    target_ws: str,
    name: str,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Create missing target peers/collections as full-column copies from source.

    Existing rows in the target workspace are left untouched. Returns lists of
    peer names and (observer, observed) pairs that were created.
    """
    created_peers: list[str] = []
    for pname in sorted(await _required_peers(session, source_ws, name)):
        exists = await session.scalar(
            select(models.Peer).where(
                models.Peer.workspace_name == target_ws,
                models.Peer.name == pname,
            )
        )
        if exists is None:
            src = await session.scalar(
                select(models.Peer).where(
                    models.Peer.workspace_name == source_ws,
                    models.Peer.name == pname,
                )
            )
            if src is not None:
                session.add(_copy_row(src, models.Peer, workspace_name=target_ws))
                created_peers.append(pname)

    # Flush peers before collections: Collection has FK to peers in same workspace.
    if created_peers:
        await session.flush()

    created_cols: list[tuple[str, str]] = []
    for obs, observed in sorted(await _required_collections(session, source_ws, name)):
        exists = await session.scalar(
            select(models.Collection).where(
                models.Collection.workspace_name == target_ws,
                models.Collection.observer == obs,
                models.Collection.observed == observed,
            )
        )
        if exists is None:
            src = await session.scalar(
                select(models.Collection).where(
                    models.Collection.workspace_name == source_ws,
                    models.Collection.observer == obs,
                    models.Collection.observed == observed,
                )
            )
            if src is not None:
                session.add(_copy_row(src, models.Collection, workspace_name=target_ws))
                created_cols.append((obs, observed))

    return created_peers, created_cols


async def _session_fk_constraints(session: AsyncSession) -> list[tuple[str, str]]:
    """Return ``(child_table, conname)`` for every FK whose ``confrelid`` is
    ``sessions`` (the composite child FKs plus ``queue.session_id``)."""
    rows = await session.execute(
        text(
            "SELECT conrelid::regclass::text AS child, conname "
            "FROM pg_constraint "
            "WHERE contype='f' AND confrelid='sessions'::regclass"
        )
    )
    return [(r.child, r.conname) for r in rows]


async def relocate_in_place(
    session: AsyncSession,
    source_ws: str,
    target_ws: str,
    source_name: str,
    target_name: str,
) -> None:
    """Move a session row and its children to a new ``(name, workspace_name)``
    in place, preserving every ``id``/``public_id``.

    Uses transaction-local deferrable constraints so the parent and child
    composite FKs are checked together at drain time rather than per-statement.
    """
    fks = await _session_fk_constraints(session)
    # 1. make the session FKs deferrable for this transaction
    for child, conname in fks:
        await session.execute(
            text(f'ALTER TABLE {child} ALTER CONSTRAINT "{conname}" DEFERRABLE')
        )
    await session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    # 2. move the parent row in place (id/created_at/metadata preserved)
    await session.execute(
        update(models.Session)
        .where(
            models.Session.workspace_name == source_ws,
            models.Session.name == source_name,
        )
        .values(workspace_name=target_ws, name=target_name)
    )
    # 3. move children in place (public_id/id preserved -> no CASCADE).
    #    queue is intentionally excluded from _CHILD_MODELS (handled later).
    for model in _CHILD_MODELS:
        await session.execute(
            update(model)
            .where(
                model.workspace_name == source_ws,
                model.session_name == source_name,
            )
            .values(workspace_name=target_ws, session_name=target_name)
        )
    # 4. drain deferred checks (now consistent) BEFORE restoring NOT DEFERRABLE
    await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    for child, conname in fks:
        await session.execute(
            text(f'ALTER TABLE {child} ALTER CONSTRAINT "{conname}" NOT DEFERRABLE')
        )


async def clear_session_queue(
    session: AsyncSession, ws: str, name: str, force: bool
) -> int:
    """Delete all queue rows for ``session``/``ws``/``name``.

    If any rows are unprocessed and ``force`` is False, raises ``MoveError``
    rather than deleting.  Returns the count of rows deleted.  Queue rows are
    transient work-state whose ``work_unit_key``/``payload`` embed the old
    workspace identity, so they are never repointed — only cleared.
    """
    sess = await _session_row(session, ws, name)
    if sess is None:
        return 0
    pending = await session.scalar(
        select(func.count())
        .select_from(models.QueueItem)
        .where(
            models.QueueItem.session_id == sess.id,
            models.QueueItem.processed.is_(False),
        )
    )
    if pending and not force:
        raise MoveError(
            f"session '{name}' has {pending} pending queue items; "
            f"re-run with --force-clear-queue to delete them"
        )
    result = await session.execute(
        delete(models.QueueItem).where(models.QueueItem.session_id == sess.id)
    )
    return result.rowcount or 0


async def cross_boundary_premises(
    session: AsyncSession, ws: str, moved_names: set[str]
) -> list[str]:
    """Return premise doc ids cited by docs in moved sessions that originate
    outside the move set (peer-global or in a non-moved session).

    Co-moved premises (``session_name in moved_names``) are NOT flagged.
    This is a read-only report.
    """
    # Collect all premise ids cited by docs in the moved sessions
    rows = await session.scalars(
        select(models.Document.source_ids).where(
            models.Document.workspace_name == ws,
            models.Document.session_name.in_(moved_names),
        )
    )
    premise_ids: set[str] = set()
    for sid_list in rows:
        if sid_list:
            premise_ids.update(sid_list)
    if not premise_ids:
        return []
    # Find premises that are peer-global or in a non-moved session
    flagged: list[str] = []
    prem_rows = await session.execute(
        select(models.Document.id, models.Document.session_name).where(
            models.Document.workspace_name == ws,
            models.Document.id.in_(premise_ids),
        )
    )
    for doc_id, sess_name in prem_rows.all():
        if sess_name is None or sess_name not in moved_names:
            flagged.append(doc_id)
    return flagged


async def _assert_integrity(session: AsyncSession, target_ws: str) -> None:
    """Raise MoveError if any child row has an unparented (session_name, workspace_name)
    or any queue row references a missing session."""
    for model in _CHILD_MODELS:
        tname = getattr(model, "__tablename__", None) or model.__table__.name
        orphan = await session.scalar(
            text(
                f"SELECT 1 FROM {tname} c "
                f"LEFT JOIN sessions s ON s.name=c.session_name AND s.workspace_name=c.workspace_name "
                f"WHERE s.name IS NULL LIMIT 1"
            )
        )
        if orphan:
            raise MoveError(f"integrity: orphaned rows in {tname}")
    dangling = await session.scalar(
        text(
            "SELECT 1 FROM queue q LEFT JOIN sessions s ON s.id=q.session_id "
            "WHERE q.session_id IS NOT NULL AND s.id IS NULL LIMIT 1"
        )
    )
    if dangling:
        raise MoveError("integrity: queue rows reference a missing session")


async def apply_moves(
    session: AsyncSession,
    source_ws: str,
    target_ws: str,
    plans: list[SessionPlan],
    force_clear_queue: bool,
) -> None:
    """Apply the given plans: for each plan ensure dependencies, clear queue,
    relocate in place, then flush and assert integrity.

    The caller controls the outer transaction (commit/rollback).
    """
    for plan in plans:
        await ensure_dependencies(session, source_ws, target_ws, plan.source_name)
        await clear_session_queue(
            session, source_ws, plan.source_name, force=force_clear_queue
        )
        await relocate_in_place(
            session, source_ws, target_ws, plan.source_name, plan.target_name
        )
    await session.flush()
    await _assert_integrity(session, target_ws)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Losslessly move sessions between Honcho workspaces."
    )
    p.add_argument("--from", dest="source", required=True)
    p.add_argument("--to", dest="target", required=True)
    p.add_argument("--session", action="append", required=True, help="repeatable")
    p.add_argument("--on-collision", choices=["rename", "skip"], default="rename")
    p.add_argument("--rename-suffix", default="-from-{source}")
    p.add_argument("--apply", action="store_true", help="default: dry-run")
    p.add_argument("--force-clear-queue", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    return p


def _pg_dump(out_path: str) -> None:
    import subprocess

    from src.config import settings

    uri = settings.DB.CONNECTION_URI
    # strip the +psycopg driver suffix for libpq / pg_dump
    libpq = uri.replace("postgresql+psycopg", "postgresql")
    subprocess.run(["pg_dump", "--dbname", libpq, "--file", out_path], check=True)


def _print_plans(plans: list[SessionPlan], apply: bool) -> None:
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"[{mode}] {len(plans)} session(s):")
    for p in plans:
        rn = f" -> {p.target_name} (renamed)" if p.renamed else ""
        print(
            f"  {p.source_name}{rn}: {p.messages} msgs, {p.documents} docs, "
            f"{p.embeddings} embeddings; create peers={p.peers_to_create} "
            f"collections={p.collections_to_create}; queue={p.queue_rows}"
        )
        if p.cross_boundary_premises:
            print(
                f"    WARNING cross-boundary premises (will dangle): "
                f"{p.cross_boundary_premises}"
            )


async def main_async(args) -> int:
    from src.db import SessionLocal

    async with SessionLocal() as session:
        plans = await plan_moves(
            session,
            args.source,
            args.target,
            args.session,
            args.on_collision,
            args.rename_suffix,
        )
        moved = {p.source_name for p in plans}
        for p in plans:
            p.cross_boundary_premises = await cross_boundary_premises(
                session, args.source, moved
            )
        _print_plans(plans, apply=args.apply)
        if not args.apply:
            return 0
        if not args.no_backup:
            import datetime

            path = f"/tmp/honcho-backup-{datetime.datetime.utcnow():%Y%m%dT%H%M%SZ}.sql"
            _pg_dump(path)
            print(f"backup written: {path}")
        async with session.begin():
            await apply_moves(
                session, args.source, args.target, plans, args.force_clear_queue
            )
        print("move applied.")
        return 0


def main() -> int:
    return asyncio.run(main_async(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())


async def plan_moves(
    session: AsyncSession,
    source_ws: str,
    target_ws: str,
    names: list[str],
    on_collision: str = "rename",
    rename_suffix: str = "-from-{source}",
) -> list[SessionPlan]:
    if source_ws == target_ws:
        raise MoveError("source and target are the same workspace")
    if not await _workspace_exists(session, source_ws):
        raise MoveError(f"source workspace '{source_ws}' not found")
    if not await _workspace_exists(session, target_ws):
        raise MoveError(f"target workspace '{target_ws}' not found")

    plans: list[SessionPlan] = []
    for name in names:
        src = await _session_row(session, source_ws, name)
        if src is None:
            raise MoveError(f"session '{name}' not found in workspace '{source_ws}'")
        target_name, renamed, skip = await _resolve_target_name(
            session, target_ws, name, on_collision, rename_suffix, source_ws
        )
        if skip:
            continue
        plans.append(
            SessionPlan(
                source_name=name,
                target_name=target_name,
                renamed=renamed,
                messages=await _count(session, models.Message, source_ws, name),
                embeddings=await _count(
                    session, models.MessageEmbedding, source_ws, name
                ),
                documents=await _count(session, models.Document, source_ws, name),
            )
        )
    return plans
