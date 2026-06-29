# Cross-Workspace Session Move — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `scripts/move_session_workspace.py`, an admin tool that losslessly moves named sessions from one Honcho workspace to another (a "workspace bleed" fix), without deleting data.

**Architecture:** A thin argparse CLI wrapping pure, async, individually-testable functions. The move runs in one transaction: validate → collision-resolve (rename) → full-column-copy missing target peers/collections → in-place relocate of the session + children using transaction-local **deferrable constraints** (id-preserving) → clear transient queue → assert integrity. Dry-run by default; auto `pg_dump` before `--apply`.

**Tech Stack:** Python 3.10+, async SQLAlchemy (`src.db.SessionLocal`), `src.models`, argparse, pytest + pytest-asyncio (`asyncio_mode=auto`), the `db_session` fixture (function-scoped `AsyncSession` on a per-worker test DB).

**Spec:** `docs/superpowers/specs/2026-06-29-cross-workspace-session-move-design.md` (Gate-1 cleared).

## Global Constraints

- **Lossless** = every moved row keeps all non-derived columns + content; children keep `id`/`public_id`; the primary/superuser in-place paths preserve the session `id`; the no-privilege fallback reassigns it.
- **Move surface:** `sessions` (parent) + `messages`, `message_embeddings`, `documents`, `session_peers` (children, composite FK `(session_name, workspace_name)`); `queue` (by `id`, transient — cleared not moved); plus referenced `peers` and `(observer, observed)` collections.
- **Composite FKs are NOT DEFERRABLE / NO ACTION**; `message_embeddings.message_id → messages.public_id` is `ON DELETE CASCADE` → children are UPDATEd in place, never recreated.
- **FK constraint names resolved from `pg_constraint` at runtime** (SQLAlchemy does not name them deterministically).
- **Dry-run is the default.** `--apply` runs `pg_dump` first, then one transaction.
- Run files through `uv run ruff format` + `uv run ruff check` before each commit; tests via `uv run pytest`.
- Test file: `tests/scripts/test_move_session_workspace.py`. Import logic via `from scripts.move_session_workspace import …` (scripts is a package — `scripts/__init__.py` exists).

---

### Task 1: CLI scaffold + validation + dry-run plan for a clean session

**Files:**
- Create: `scripts/move_session_workspace.py`
- Test: `tests/scripts/test_move_session_workspace.py`

**Interfaces:**
- Produces:
  - `@dataclass SessionPlan` with fields: `source_name: str`, `target_name: str`, `renamed: bool`, `messages: int`, `embeddings: int`, `documents: int`, `peers_to_create: list[str]`, `collections_to_create: list[tuple[str, str]]`, `queue_rows: int`, `cross_boundary_premises: list[str]`.
  - `async def plan_moves(session: AsyncSession, source_ws: str, target_ws: str, names: list[str], on_collision: str = "rename", rename_suffix: str = "-from-{source}") -> list[SessionPlan]` — read-only; raises `MoveError` on validation failure.
  - `class MoveError(Exception)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/scripts/test_move_session_workspace.py
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from src import models
from scripts.move_session_workspace import plan_moves, MoveError


async def _mk_workspace(db: AsyncSession, name: str) -> None:
    db.add(models.Workspace(name=name))
    await db.flush()


async def _mk_session_with_messages(db: AsyncSession, ws: str, name: str, peer: str, n: int) -> None:
    db.add(models.Peer(name=peer, workspace_name=ws))
    db.add(models.Session(name=name, workspace_name=ws))
    await db.flush()
    for i in range(n):
        db.add(models.Message(session_name=name, workspace_name=ws, peer_name=peer, content=f"m{i}"))
    await db.flush()


@pytest.mark.asyncio
async def test_plan_clean_session_counts(db_session: AsyncSession):
    await _mk_workspace(db_session, "personal")
    await _mk_workspace(db_session, "highway")
    await _mk_session_with_messages(db_session, "personal", "s1", "robsherman", 3)

    plans = await plan_moves(db_session, "personal", "highway", ["s1"])

    assert len(plans) == 1
    assert plans[0].source_name == "s1"
    assert plans[0].target_name == "s1"
    assert plans[0].renamed is False
    assert plans[0].messages == 3


@pytest.mark.asyncio
async def test_plan_rejects_same_workspace(db_session: AsyncSession):
    await _mk_workspace(db_session, "personal")
    with pytest.raises(MoveError, match="same workspace"):
        await plan_moves(db_session, "personal", "personal", ["s1"])


@pytest.mark.asyncio
async def test_plan_rejects_missing_session(db_session: AsyncSession):
    await _mk_workspace(db_session, "personal")
    await _mk_workspace(db_session, "highway")
    with pytest.raises(MoveError, match="not found"):
        await plan_moves(db_session, "personal", "highway", ["nope"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scripts/test_move_session_workspace.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.move_session_workspace'`.

- [ ] **Step 3: Write minimal implementation**

```python
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
    r = await session.scalar(select(models.Workspace.name).where(models.Workspace.name == name))
    return r is not None


async def _session_row(session: AsyncSession, ws: str, name: str) -> models.Session | None:
    return await session.scalar(
        select(models.Session).where(
            models.Session.workspace_name == ws, models.Session.name == name
        )
    )


async def _count(session: AsyncSession, model, ws: str, name: str) -> int:
    return await session.scalar(
        select(func.count()).select_from(model).where(
            model.workspace_name == ws, model.session_name == name
        )
    ) or 0


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
        plans.append(
            SessionPlan(
                source_name=name,
                target_name=name,
                renamed=False,
                messages=await _count(session, models.Message, source_ws, name),
                embeddings=await _count(session, models.MessageEmbedding, source_ws, name),
                documents=await _count(session, models.Document, source_ws, name),
            )
        )
    return plans
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/scripts/test_move_session_workspace.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
uv run ruff format scripts/move_session_workspace.py tests/scripts/test_move_session_workspace.py
uv run ruff check scripts/move_session_workspace.py
git add scripts/move_session_workspace.py tests/scripts/test_move_session_workspace.py
git commit -m "feat(scripts): move_session_workspace plan/validation skeleton"
```

---

### Task 2: Collision detection + rename / skip

**Files:**
- Modify: `scripts/move_session_workspace.py` (extend `plan_moves`)
- Test: `tests/scripts/test_move_session_workspace.py`

**Interfaces:**
- Produces: `async def _resolve_target_name(session, target_ws, name, on_collision, rename_suffix, source_ws) -> tuple[str, bool, bool]` returning `(target_name, renamed, skip)`.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_plan_renames_on_collision(db_session: AsyncSession):
    await _mk_workspace(db_session, "personal")
    await _mk_workspace(db_session, "highway")
    await _mk_session_with_messages(db_session, "personal", "maca", "robsherman", 2)
    await _mk_session_with_messages(db_session, "highway", "maca", "robsherman", 5)  # collision

    plans = await plan_moves(db_session, "personal", "highway", ["maca"])
    assert plans[0].target_name == "maca-from-personal"
    assert plans[0].renamed is True


@pytest.mark.asyncio
async def test_plan_skip_mode_leaves_collision(db_session: AsyncSession):
    await _mk_workspace(db_session, "personal")
    await _mk_workspace(db_session, "highway")
    await _mk_session_with_messages(db_session, "personal", "maca", "robsherman", 2)
    await _mk_session_with_messages(db_session, "highway", "maca", "robsherman", 5)

    plans = await plan_moves(db_session, "personal", "highway", ["maca"], on_collision="skip")
    assert plans == []  # skipped, nothing to do
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scripts/test_move_session_workspace.py -k collision -v`
Expected: FAIL — target_name is "maca", not renamed.

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/move_session_workspace.py`:

```python
async def _resolve_target_name(
    session: AsyncSession, target_ws: str, name: str,
    on_collision: str, rename_suffix: str, source_ws: str,
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
```

In `plan_moves`, replace the `plans.append(...)` block's name fields:

```python
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
                embeddings=await _count(session, models.MessageEmbedding, source_ws, name),
                documents=await _count(session, models.Document, source_ws, name),
            )
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/scripts/test_move_session_workspace.py -k "collision or skip" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format scripts/ tests/scripts/ && uv run ruff check scripts/move_session_workspace.py
git add -A && git commit -m "feat(scripts): collision rename/skip resolution"
```

---

### Task 3: Enumerate + full-column-copy missing target peers & collections

**Files:**
- Modify: `scripts/move_session_workspace.py`
- Test: `tests/scripts/test_move_session_workspace.py`

**Interfaces:**
- Produces:
  - `async def _required_peers(session, ws, name) -> set[str]` — union of peers from messages, message_embeddings, session_peers, documents (observer/observed), collections (observer/observed) for the session.
  - `async def _required_collections(session, ws, name) -> set[tuple[str, str]]` — `(observer, observed)` from the session's documents.
  - `async def ensure_dependencies(session, source_ws, target_ws, name) -> tuple[list[str], list[tuple[str, str]]]` — creates missing target peers/collections as full-column copies; returns lists of what it created. Existing rows are left untouched.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_ensure_dependencies_copies_missing_peer_fullcolumn(db_session: AsyncSession):
    await _mk_workspace(db_session, "personal")
    await _mk_workspace(db_session, "highway")
    # peer with metadata in personal; session uses it
    db_session.add(models.Peer(name="robsherman", workspace_name="personal",
                               internal_metadata={"card": "x"}))
    db_session.add(models.Session(name="s1", workspace_name="personal"))
    await db_session.flush()
    db_session.add(models.Message(session_name="s1", workspace_name="personal",
                                  peer_name="robsherman", content="hi"))
    await db_session.flush()

    from scripts.move_session_workspace import ensure_dependencies
    created_peers, _ = await ensure_dependencies(db_session, "personal", "highway", "s1")
    await db_session.flush()

    assert created_peers == ["robsherman"]
    moved = await db_session.scalar(
        select(models.Peer).where(models.Peer.workspace_name == "highway",
                                  models.Peer.name == "robsherman"))
    assert moved is not None
    assert moved.internal_metadata == {"card": "x"}  # full-column copy preserved peer card


@pytest.mark.asyncio
async def test_ensure_dependencies_leaves_existing_peer_untouched(db_session: AsyncSession):
    await _mk_workspace(db_session, "personal")
    await _mk_workspace(db_session, "highway")
    db_session.add(models.Peer(name="robsherman", workspace_name="personal",
                               internal_metadata={"card": "SOURCE"}))
    db_session.add(models.Peer(name="robsherman", workspace_name="highway",
                               internal_metadata={"card": "TARGET"}))
    db_session.add(models.Session(name="s1", workspace_name="personal"))
    await db_session.flush()
    db_session.add(models.Message(session_name="s1", workspace_name="personal",
                                  peer_name="robsherman", content="hi"))
    await db_session.flush()

    from scripts.move_session_workspace import ensure_dependencies
    created_peers, _ = await ensure_dependencies(db_session, "personal", "highway", "s1")
    await db_session.flush()

    assert created_peers == []  # already present, not created
    existing = await db_session.scalar(
        select(models.Peer).where(models.Peer.workspace_name == "highway",
                                  models.Peer.name == "robsherman"))
    assert existing.internal_metadata == {"card": "TARGET"}  # NOT clobbered
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scripts/test_move_session_workspace.py -k dependencies -v`
Expected: FAIL — `ensure_dependencies` not defined.

- [ ] **Step 3: Write minimal implementation**

```python
def _copy_row(src_obj, model, **overrides):
    """Full-column copy of an ORM row into a new instance, with overrides."""
    data = {c.name: getattr(src_obj, c.name) for c in model.__table__.columns}
    data.pop("id", None)  # let a fresh PK be generated
    data.update(overrides)
    return model(**data)


async def _required_peers(session: AsyncSession, ws: str, name: str) -> set[str]:
    peers: set[str] = set()
    peers.update(await session.scalars(
        select(models.Message.peer_name.distinct()).where(
            models.Message.workspace_name == ws, models.Message.session_name == name)))
    peers.update(await session.scalars(
        select(models.MessageEmbedding.peer_name.distinct()).where(
            models.MessageEmbedding.workspace_name == ws,
            models.MessageEmbedding.session_name == name)))
    peers.update(await session.scalars(
        select(models.SessionPeer.peer_name.distinct()).where(
            models.SessionPeer.workspace_name == ws, models.SessionPeer.session_name == name)))
    for col in (models.Document.observer, models.Document.observed):
        peers.update(await session.scalars(
            select(col.distinct()).where(
                models.Document.workspace_name == ws, models.Document.session_name == name)))
    peers.discard(None)
    return peers


async def _required_collections(session: AsyncSession, ws: str, name: str) -> set[tuple[str, str]]:
    rows = await session.execute(
        select(models.Document.observer, models.Document.observed).where(
            models.Document.workspace_name == ws, models.Document.session_name == name).distinct())
    return {(o, d) for o, d in rows.all()}


async def ensure_dependencies(session, source_ws, target_ws, name):
    created_peers: list[str] = []
    for pname in sorted(await _required_peers(session, source_ws, name)):
        if await session.scalar(select(models.Peer).where(
                models.Peer.workspace_name == target_ws, models.Peer.name == pname)) is None:
            src = await session.scalar(select(models.Peer).where(
                models.Peer.workspace_name == source_ws, models.Peer.name == pname))
            if src is not None:
                session.add(_copy_row(src, models.Peer, workspace_name=target_ws))
                created_peers.append(pname)
    created_cols: list[tuple[str, str]] = []
    for obs, observed in sorted(await _required_collections(session, source_ws, name)):
        if await session.scalar(select(models.Collection).where(
                models.Collection.workspace_name == target_ws,
                models.Collection.observer == obs,
                models.Collection.observed == observed)) is None:
            src = await session.scalar(select(models.Collection).where(
                models.Collection.workspace_name == source_ws,
                models.Collection.observer == obs, models.Collection.observed == observed))
            if src is not None:
                session.add(_copy_row(src, models.Collection, workspace_name=target_ws))
                created_cols.append((obs, observed))
    return created_peers, created_cols
```

> Confirmed against `src/models.py`: `Peer` columns include `internal_metadata`/`configuration`/`h_metadata`; `Document` has `observer`/`observed`/`session_name`; `Collection` has `observer`/`observed`/`workspace_name`. `_copy_row` pops `id` so the nanoid default generates a fresh PK (peers/collections are referenced by `(name|observer/observed, workspace_name)`, not by `id`).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/scripts/test_move_session_workspace.py -k dependencies -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
uv run ruff format scripts/ tests/scripts/ && uv run ruff check scripts/move_session_workspace.py
git add -A && git commit -m "feat(scripts): full-column copy of missing target peers/collections"
```

---

### Task 4: In-place relocate via transaction-local deferrable constraints (the core)

**Files:**
- Modify: `scripts/move_session_workspace.py`
- Test: `tests/scripts/test_move_session_workspace.py`

**Interfaces:**
- Produces:
  - `async def _session_fk_constraints(session) -> list[tuple[str, str]]` — `(child_table, conname)` for every FK whose `confrelid` is `sessions`, from `pg_constraint`.
  - `async def relocate_in_place(session, source_ws, target_ws, source_name, target_name) -> None` — defers the session FKs, UPDATEs session + children, drains with `SET CONSTRAINTS ALL IMMEDIATE`, restores `NOT DEFERRABLE`. Preserves session `id`.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_relocate_preserves_id_and_moves_children(db_session: AsyncSession):
    await _mk_workspace(db_session, "personal")
    await _mk_workspace(db_session, "highway")
    db_session.add(models.Peer(name="robsherman", workspace_name="highway"))  # target peer exists
    db_session.add(models.Session(name="s1", workspace_name="personal"))
    await db_session.flush()
    src_sess = await db_session.scalar(select(models.Session).where(
        models.Session.workspace_name == "personal", models.Session.name == "s1"))
    src_id, src_created = src_sess.id, src_sess.created_at
    db_session.add(models.Peer(name="robsherman", workspace_name="personal"))
    await db_session.flush()
    db_session.add(models.Message(session_name="s1", workspace_name="personal",
                                  peer_name="robsherman", content="hi"))
    await db_session.flush()

    from scripts.move_session_workspace import relocate_in_place, ensure_dependencies
    await ensure_dependencies(db_session, "personal", "highway", "s1")
    await relocate_in_place(db_session, "personal", "highway", "s1", "s1")
    await db_session.flush()

    moved = await db_session.scalar(select(models.Session).where(
        models.Session.workspace_name == "highway", models.Session.name == "s1"))
    assert moved is not None
    assert moved.id == src_id          # id preserved
    assert moved.created_at == src_created
    assert await _count(db_session, models.Message, "highway", "s1") == 1
    assert await _count(db_session, models.Message, "personal", "s1") == 0
    # no orphaned source session row
    assert await _session_row_helper(db_session, "personal", "s1") is None
```

Add the helper at the top of the test module:

```python
from scripts.move_session_workspace import _session_row as _session_row_helper
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scripts/test_move_session_workspace.py -k relocate -v`
Expected: FAIL — `relocate_in_place` not defined.

- [ ] **Step 3: Write minimal implementation**

```python
from sqlalchemy import text, update

_CHILD_MODELS = (models.Message, models.MessageEmbedding, models.Document, models.SessionPeer)


async def _session_fk_constraints(session: AsyncSession) -> list[tuple[str, str]]:
    rows = await session.execute(text(
        "SELECT conrelid::regclass::text AS child, conname "
        "FROM pg_constraint "
        "WHERE contype='f' AND confrelid='sessions'::regclass"))
    return [(r.child, r.conname) for r in rows]


async def relocate_in_place(session, source_ws, target_ws, source_name, target_name) -> None:
    fks = await _session_fk_constraints(session)
    # 1. make the session FKs deferrable for this transaction
    for child, conname in fks:
        await session.execute(text(f'ALTER TABLE {child} ALTER CONSTRAINT "{conname}" DEFERRABLE'))
    await session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
    # 2. move the parent row in place (id/created_at/metadata preserved)
    await session.execute(
        update(models.Session)
        .where(models.Session.workspace_name == source_ws, models.Session.name == source_name)
        .values(workspace_name=target_ws, name=target_name))
    # 3. move children in place (public_id/id preserved → no CASCADE)
    for model in _CHILD_MODELS:
        await session.execute(
            update(model)
            .where(model.workspace_name == source_ws, model.session_name == source_name)
            .values(workspace_name=target_ws, session_name=target_name))
    # 4. drain deferred checks (now consistent) BEFORE restoring NOT DEFERRABLE
    await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    for child, conname in fks:
        await session.execute(text(f'ALTER TABLE {child} ALTER CONSTRAINT "{conname}" NOT DEFERRABLE'))
```

> Confirmed: `Document` has both `workspace_name` (collection FK side) and `session_name` (nullable); the single `.values(workspace_name=…, session_name=…)` update covers both.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/scripts/test_move_session_workspace.py -k relocate -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format scripts/ tests/scripts/ && uv run ruff check scripts/move_session_workspace.py
git add -A && git commit -m "feat(scripts): id-preserving in-place relocate via deferrable constraints"
```

---

### Task 5: Clear transient queue rows (never repoint)

**Files:**
- Modify: `scripts/move_session_workspace.py`
- Test: `tests/scripts/test_move_session_workspace.py`

**Interfaces:**
- Produces: `async def clear_session_queue(session, ws, name, force: bool) -> int` — deletes the session's `queue` rows; if any are unprocessed and `force` is False, raises `MoveError`. Returns rows deleted.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_clear_queue_deletes_rows(db_session: AsyncSession):
    await _mk_workspace(db_session, "personal")
    db_session.add(models.Session(name="s1", workspace_name="personal"))
    await db_session.flush()
    db_session.add(models.Message(session_name="s1", workspace_name="personal",
                                  peer_name=None, content="x"))
    await db_session.flush()
    # seed a processed queue row for the session
    sess = await db_session.scalar(select(models.Session).where(
        models.Session.workspace_name == "personal", models.Session.name == "s1"))
    db_session.add(models.QueueItem(session_id=sess.id, workspace_name="personal",
                                    payload={}, processed=True))
    await db_session.flush()

    from scripts.move_session_workspace import clear_session_queue
    deleted = await clear_session_queue(db_session, "personal", "s1", force=False)
    await db_session.flush()
    assert deleted == 1
```

> Confirmed: `models.QueueItem` (table `queue`) has `session_id`, `workspace_name`, `payload`, and a boolean `processed` (also `work_unit_key`, `task_type`, `message_id`).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scripts/test_move_session_workspace.py -k queue -v`
Expected: FAIL — `clear_session_queue` not defined.

- [ ] **Step 3: Write minimal implementation**

```python
from sqlalchemy import delete


async def clear_session_queue(session, ws, name, force: bool) -> int:
    sess = await _session_row(session, ws, name)
    if sess is None:
        return 0
    pending = await session.scalar(
        select(func.count()).select_from(models.QueueItem).where(
            models.QueueItem.session_id == sess.id,
            models.QueueItem.processed.is_(False)))
    if pending and not force:
        raise MoveError(
            f"session '{name}' has {pending} pending queue items; "
            f"re-run with --force-clear-queue to delete them")
    result = await session.execute(
        delete(models.QueueItem).where(models.QueueItem.session_id == sess.id))
    return result.rowcount or 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/scripts/test_move_session_workspace.py -k queue -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format scripts/ tests/scripts/ && uv run ruff check scripts/move_session_workspace.py
git add -A && git commit -m "feat(scripts): clear transient queue rows (assert-or-force)"
```

---

### Task 6: Cross-boundary `source_ids` premise report

**Files:**
- Modify: `scripts/move_session_workspace.py`
- Test: `tests/scripts/test_move_session_workspace.py`

**Interfaces:**
- Produces: `async def cross_boundary_premises(session, ws, moved_names: set[str]) -> list[str]` — for documents in the moved sessions, return premise doc ids whose source document is peer-global (`session_name IS NULL`) or in a session NOT in `moved_names`. Co-moved sessions are NOT flagged.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_cross_boundary_premises_flags_only_outside_move_set(db_session: AsyncSession):
    await _mk_workspace(db_session, "personal")
    db_session.add(models.Peer(name="p", workspace_name="personal"))
    for s in ("a", "b", "other"):
        db_session.add(models.Session(name=s, workspace_name="personal"))
    await db_session.flush()
    # premise docs
    prem_co = models.Document(workspace_name="personal", session_name="b",
                              observer="p", observed="p", content="co", source_ids=[])
    prem_out = models.Document(workspace_name="personal", session_name="other",
                               observer="p", observed="p", content="out", source_ids=[])
    db_session.add_all([prem_co, prem_out])
    await db_session.flush()
    # a conclusion in session "a" citing both premises
    db_session.add(models.Document(workspace_name="personal", session_name="a",
                                   observer="p", observed="p", content="concl",
                                   source_ids=[prem_co.id, prem_out.id]))
    await db_session.flush()

    from scripts.move_session_workspace import cross_boundary_premises
    flagged = await cross_boundary_premises(db_session, "personal", {"a", "b"})
    assert prem_out.id in flagged       # "other" is outside the move set → flagged
    assert prem_co.id not in flagged    # "b" is co-moved → not flagged
```

> Confirmed: `Document.source_ids` is a JSONB list of premise document ids; `Document.id` is the PK referenced by those ids.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scripts/test_move_session_workspace.py -k premise -v`
Expected: FAIL — not defined.

- [ ] **Step 3: Write minimal implementation**

```python
async def cross_boundary_premises(session, ws, moved_names: set[str]) -> list[str]:
    # collect all premise ids cited by docs in the moved sessions
    rows = await session.scalars(
        select(models.Document.source_ids).where(
            models.Document.workspace_name == ws,
            models.Document.session_name.in_(moved_names)))
    premise_ids: set[str] = set()
    for sid_list in rows:
        if sid_list:
            premise_ids.update(sid_list)
    if not premise_ids:
        return []
    # find premises that are peer-global or in a non-moved session
    flagged: list[str] = []
    prem_rows = await session.execute(
        select(models.Document.id, models.Document.session_name).where(
            models.Document.workspace_name == ws, models.Document.id.in_(premise_ids)))
    for doc_id, sess_name in prem_rows.all():
        if sess_name is None or sess_name not in moved_names:
            flagged.append(doc_id)
    return flagged
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/scripts/test_move_session_workspace.py -k premise -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format scripts/ tests/scripts/ && uv run ruff check scripts/move_session_workspace.py
git add -A && git commit -m "feat(scripts): cross-boundary source_ids premise report"
```

---

### Task 7: `apply_moves` orchestration + integrity assertion + dry-run guarantee

**Files:**
- Modify: `scripts/move_session_workspace.py`
- Test: `tests/scripts/test_move_session_workspace.py`

**Interfaces:**
- Produces:
  - `async def apply_moves(session, source_ws, target_ws, plans, force_clear_queue: bool) -> None` — for each plan: `ensure_dependencies`, `clear_session_queue`, `relocate_in_place`, then `_assert_integrity`. Caller controls the transaction (commit/rollback).
  - `async def _assert_integrity(session, target_ws) -> None` — raises `MoveError` if any child row has an unparented `(session_name, workspace_name)` or any `queue.session_id` lacks a session.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_apply_then_integrity_clean(db_session: AsyncSession):
    await _mk_workspace(db_session, "personal")
    await _mk_workspace(db_session, "highway")
    await _mk_session_with_messages(db_session, "personal", "s1", "robsherman", 2)

    from scripts.move_session_workspace import plan_moves, apply_moves
    plans = await plan_moves(db_session, "personal", "highway", ["s1"])
    await apply_moves(db_session, "personal", "highway", plans, force_clear_queue=True)
    await db_session.flush()

    assert await _count(db_session, models.Message, "highway", "s1") == 2
    assert await _count(db_session, models.Message, "personal", "s1") == 0


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(db_session: AsyncSession):
    await _mk_workspace(db_session, "personal")
    await _mk_workspace(db_session, "highway")
    await _mk_session_with_messages(db_session, "personal", "s1", "robsherman", 2)

    from scripts.move_session_workspace import plan_moves
    before = await _count(db_session, models.Message, "personal", "s1")
    await plan_moves(db_session, "personal", "highway", ["s1"])  # plan only, no apply
    after = await _count(db_session, models.Message, "personal", "s1")
    assert before == after == 2  # plan_moves is read-only
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/scripts/test_move_session_workspace.py -k "apply or dry_run" -v`
Expected: FAIL — `apply_moves` not defined.

- [ ] **Step 3: Write minimal implementation**

```python
async def _assert_integrity(session, target_ws) -> None:
    for model in _CHILD_MODELS:
        orphan = await session.scalar(text(
            f"SELECT 1 FROM {model.__tablename__} c "
            f"LEFT JOIN sessions s ON s.name=c.session_name AND s.workspace_name=c.workspace_name "
            f"WHERE s.name IS NULL LIMIT 1"))
        if orphan:
            raise MoveError(f"integrity: orphaned rows in {model.__tablename__}")
    dangling = await session.scalar(text(
        "SELECT 1 FROM queue q LEFT JOIN sessions s ON s.id=q.session_id "
        "WHERE q.session_id IS NOT NULL AND s.id IS NULL LIMIT 1"))
    if dangling:
        raise MoveError("integrity: queue rows reference a missing session")


async def apply_moves(session, source_ws, target_ws, plans, force_clear_queue: bool) -> None:
    for plan in plans:
        await ensure_dependencies(session, source_ws, target_ws, plan.source_name)
        await clear_session_queue(session, source_ws, plan.source_name, force=force_clear_queue)
        await relocate_in_place(session, source_ws, target_ws, plan.source_name, plan.target_name)
    await session.flush()
    await _assert_integrity(session, target_ws)
```

> Confirmed table names: `messages`, `message_embeddings`, `documents`, `session_peers`, `sessions`, `queue`. `_assert_integrity` reads `model.__tablename__` directly, so it stays correct regardless.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/scripts/test_move_session_workspace.py -k "apply or dry_run" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format scripts/ tests/scripts/ && uv run ruff check scripts/move_session_workspace.py
git add -A && git commit -m "feat(scripts): apply orchestration + post-move integrity assertion"
```

---

### Task 8: CLI entrypoint (argparse, dry-run print, --apply + pg_dump + transaction)

**Files:**
- Modify: `scripts/move_session_workspace.py`
- Test: `tests/scripts/test_move_session_workspace.py` (arg-parsing unit test; the pg_dump/transaction wiring is verified manually + by the live cleanup)

**Interfaces:**
- Produces: `def build_parser() -> argparse.ArgumentParser`; `async def main_async(args) -> int`; `def main() -> int`.

- [ ] **Step 1: Write the failing test**

```python
def test_build_parser_defaults():
    from scripts.move_session_workspace import build_parser
    args = build_parser().parse_args(
        ["--from", "personal", "--to", "highway", "--session", "s1"])
    assert args.source == "personal" and args.target == "highway"
    assert args.session == ["s1"]
    assert args.apply is False                 # dry-run default
    assert args.on_collision == "rename"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/scripts/test_move_session_workspace.py -k parser -v`
Expected: FAIL — `build_parser` not defined.

- [ ] **Step 3: Write minimal implementation**

```python
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Losslessly move sessions between Honcho workspaces.")
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
    from src.config import settings  # confirm config import path
    uri = settings.DB.CONNECTION_URI  # confirm attribute path to the DB URI
    # strip the +psycopg driver suffix for pg_dump
    libpq = uri.replace("postgresql+psycopg", "postgresql")
    subprocess.run(["pg_dump", "--dbname", libpq, "--file", out_path], check=True)


async def main_async(args) -> int:
    from src.db import SessionLocal
    async with SessionLocal() as session:
        plans = await plan_moves(session, args.source, args.target, args.session,
                                 args.on_collision, args.rename_suffix)
        moved = {p.source_name for p in plans}
        for p in plans:
            p.cross_boundary_premises = await cross_boundary_premises(session, args.source, moved)
        _print_plans(plans, apply=args.apply)
        if not args.apply:
            return 0
        if not args.no_backup:
            import datetime
            path = f"/tmp/honcho-backup-{datetime.datetime.utcnow():%Y%m%dT%H%M%SZ}.sql"
            _pg_dump(path)
            print(f"backup written: {path}")
        async with session.begin():
            await apply_moves(session, args.source, args.target, plans, args.force_clear_queue)
        print("move applied.")
        return 0


def _print_plans(plans, apply: bool) -> None:
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"[{mode}] {len(plans)} session(s):")
    for p in plans:
        rn = f" -> {p.target_name} (renamed)" if p.renamed else ""
        print(f"  {p.source_name}{rn}: {p.messages} msgs, {p.documents} docs, "
              f"{p.embeddings} embeddings; create peers={p.peers_to_create} "
              f"collections={p.collections_to_create}; queue={p.queue_rows}")
        if p.cross_boundary_premises:
            print(f"    ⚠ cross-boundary premises (will dangle): {p.cross_boundary_premises}")


def main() -> int:
    return asyncio.run(main_async(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
```

> Confirmed: the DB URI is `settings.DB.CONNECTION_URI` (`from src.config import settings`), and `SessionLocal` is importable from `src.db`. Design note (implementer's choice): to show `peers_to_create`/`collections_to_create`/`queue_rows` in the dry-run, compute them read-only during planning (a `savepoint`-and-rollback probe, or count-only queries) rather than only under `--apply`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/scripts/test_move_session_workspace.py -k parser -v`
Expected: PASS. Then run the whole file: `uv run pytest tests/scripts/test_move_session_workspace.py -v` (all green).

- [ ] **Step 5: Commit**

```bash
uv run ruff format scripts/ tests/scripts/ && uv run ruff check scripts/move_session_workspace.py
git add -A && git commit -m "feat(scripts): CLI entrypoint with dry-run, pg_dump, transactional apply"
```

---

### Task 9: Fallback (create-new-row) path for non-deferrable-capable roles

**Files:**
- Modify: `scripts/move_session_workspace.py`
- Test: `tests/scripts/test_move_session_workspace.py`

**Interfaces:**
- Produces: `async def relocate_create_new(session, source_ws, target_ws, source_name, target_name) -> None` — creates a new session row (full-column copy, new id), clears queue (already done by `apply_moves` before relocate), repoints children, deletes the old session row. `relocate_in_place` and `relocate_create_new` share the signature so `apply_moves` can select by a `strategy` argument / capability probe.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_relocate_create_new_repoints_and_deletes_old(db_session: AsyncSession):
    await _mk_workspace(db_session, "personal")
    await _mk_workspace(db_session, "highway")
    db_session.add(models.Peer(name="robsherman", workspace_name="highway"))
    db_session.add(models.Peer(name="robsherman", workspace_name="personal"))
    db_session.add(models.Session(name="s1", workspace_name="personal"))
    await db_session.flush()
    old = await db_session.scalar(select(models.Session).where(
        models.Session.workspace_name == "personal", models.Session.name == "s1"))
    old_id = old.id
    db_session.add(models.Message(session_name="s1", workspace_name="personal",
                                  peer_name="robsherman", content="hi"))
    await db_session.flush()

    from scripts.move_session_workspace import relocate_create_new
    await relocate_create_new(db_session, "personal", "highway", "s1", "s1")
    await db_session.flush()

    moved = await db_session.scalar(select(models.Session).where(
        models.Session.workspace_name == "highway", models.Session.name == "s1"))
    assert moved is not None and moved.id != old_id   # id churns on fallback
    assert await _count(db_session, models.Message, "highway", "s1") == 1
    assert await _session_row_helper(db_session, "personal", "s1") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/scripts/test_move_session_workspace.py -k create_new -v`
Expected: FAIL — not defined.

- [ ] **Step 3: Write minimal implementation**

```python
async def relocate_create_new(session, source_ws, target_ws, source_name, target_name) -> None:
    old = await _session_row(session, source_ws, source_name)
    if old is None:
        return
    # 1. new target session row (full-column copy, fresh id)
    session.add(_copy_row(old, models.Session, workspace_name=target_ws, name=target_name))
    await session.flush()
    # 2. repoint children to the new (name, workspace) — both rows exist, FK resolves
    for model in _CHILD_MODELS:
        await session.execute(
            update(model)
            .where(model.workspace_name == source_ws, model.session_name == source_name)
            .values(workspace_name=target_ws, session_name=target_name))
    # 3. delete the now-unreferenced old session row
    #    (queue rows were already cleared by apply_moves before this call)
    await session.execute(
        delete(models.Session).where(models.Session.id == old.id))
```

Wire selection into `apply_moves` (add a `strategy="in_place"` param; default in-place, `"create_new"` uses the fallback). Update the Task-7 `apply_moves` to:

```python
async def apply_moves(session, source_ws, target_ws, plans, force_clear_queue: bool,
                      strategy: str = "in_place") -> None:
    relocate = relocate_in_place if strategy == "in_place" else relocate_create_new
    for plan in plans:
        await ensure_dependencies(session, source_ws, target_ws, plan.source_name)
        await clear_session_queue(session, source_ws, plan.source_name, force=force_clear_queue)
        await relocate(session, source_ws, target_ws, plan.source_name, plan.target_name)
    await session.flush()
    await _assert_integrity(session, target_ws)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/scripts/test_move_session_workspace.py -k create_new -v`
Then the full suite: `uv run pytest tests/scripts/test_move_session_workspace.py -v` (all green).
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format scripts/ tests/scripts/ && uv run ruff check scripts/move_session_workspace.py
git add -A && git commit -m "feat(scripts): no-privilege create-new-row fallback relocate"
```

---

## Manual verification (after all tasks, before the real cleanup)

1. Bring up the dev stack; against a **scratch copy** of the DB (or the per-worker test DB), run a dry-run:
   `uv run python scripts/move_session_workspace.py --from personal --to highway --session robsherman-barry`
   Confirm the printed plan (counts, no rename for `barry`).
2. Dry-run the collision cases (`--session robsherman-maca`) and confirm rename to `robsherman-maca-from-personal`.
3. Only then run the real cleanup with `--apply` (auto `pg_dump` first), per the spec's "First real use."

## Self-review notes (author)

- Spec coverage: validation (T1), collision rename/skip (T2), full-column dependency copy + existing-untouched (T3), id-preserving relocate (T4), queue clear (T5), source_ids report (T6), apply+integrity+dry-run (T7), CLI+pg_dump+txn (T8), fallback path (T9). All spec sections map to a task.
- Tests 1–13 of the spec are covered across T1–T9 (clean move T7; rename T2; missing-peer/collection T3; dry-run T7; rollback — add as an extra assertion during execution if the harness supports injected failure; identity/integrity T4+T7; queue T5; source_ids T6; multi-session — extend T7's test with two sessions; peer-card preservation T3; existing-peer-untouched T3; fallback T9).
- Every implementation step carries real code, and all model identifiers were verified against `src/models.py` during plan authoring (`QueueItem`/`queue` with `session_id`/`processed`/`payload`; `Document.source_ids`/`session_name`/`observer`/`observed`; `Peer.internal_metadata`; `Message.content`/`peer_name`/`public_id`; `settings.DB.CONNECTION_URI`). The inline `Confirmed:` notes record those checks. No logic placeholders remain.
