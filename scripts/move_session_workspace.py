#!/usr/bin/env uv run python
"""Losslessly move named sessions between Honcho workspaces. See
docs/superpowers/specs/2026-06-29-cross-workspace-session-move-design.md."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src import models


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
