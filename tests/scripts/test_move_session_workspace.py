import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scripts.move_session_workspace import MoveError, plan_moves
from scripts.move_session_workspace import _count
from scripts.move_session_workspace import _session_row as _session_row_helper
from src import models


async def _mk_workspace(db: AsyncSession, name: str) -> None:
    db.add(models.Workspace(name=name))
    await db.flush()


async def _mk_session_with_messages(
    db: AsyncSession, ws: str, name: str, peer: str, n: int
) -> None:
    db.add(models.Peer(name=peer, workspace_name=ws))
    db.add(models.Session(name=name, workspace_name=ws))
    await db.flush()
    for i in range(n):
        db.add(
            models.Message(
                session_name=name,
                workspace_name=ws,
                peer_name=peer,
                content=f"m{i}",
                seq_in_session=i,
            )
        )
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


@pytest.mark.asyncio
async def test_plan_renames_on_collision(db_session: AsyncSession):
    await _mk_workspace(db_session, "personal")
    await _mk_workspace(db_session, "highway")
    await _mk_session_with_messages(db_session, "personal", "maca", "robsherman", 2)
    await _mk_session_with_messages(
        db_session, "highway", "maca", "robsherman", 5
    )  # collision

    plans = await plan_moves(db_session, "personal", "highway", ["maca"])
    assert plans[0].target_name == "maca-from-personal"
    assert plans[0].renamed is True


@pytest.mark.asyncio
async def test_plan_skip_mode_leaves_collision(db_session: AsyncSession):
    await _mk_workspace(db_session, "personal")
    await _mk_workspace(db_session, "highway")
    await _mk_session_with_messages(db_session, "personal", "maca", "robsherman", 2)
    await _mk_session_with_messages(db_session, "highway", "maca", "robsherman", 5)

    plans = await plan_moves(
        db_session, "personal", "highway", ["maca"], on_collision="skip"
    )
    assert plans == []  # skipped, nothing to do


@pytest.mark.asyncio
async def test_ensure_dependencies_copies_missing_peer_fullcolumn(
    db_session: AsyncSession,
):
    await _mk_workspace(db_session, "personal")
    await _mk_workspace(db_session, "highway")
    # peer with metadata in personal; session uses it
    db_session.add(
        models.Peer(
            name="robsherman",
            workspace_name="personal",
            internal_metadata={"card": "x"},
        )
    )
    db_session.add(models.Session(name="s1", workspace_name="personal"))
    await db_session.flush()
    db_session.add(
        models.Message(
            session_name="s1",
            workspace_name="personal",
            peer_name="robsherman",
            content="hi",
            seq_in_session=0,
        )
    )
    await db_session.flush()

    from scripts.move_session_workspace import ensure_dependencies

    created_peers, _ = await ensure_dependencies(
        db_session, "personal", "highway", "s1"
    )
    await db_session.flush()

    assert created_peers == ["robsherman"]
    moved = await db_session.scalar(
        select(models.Peer).where(
            models.Peer.workspace_name == "highway",
            models.Peer.name == "robsherman",
        )
    )
    assert moved is not None
    assert moved.internal_metadata == {
        "card": "x"
    }  # full-column copy preserved peer card


@pytest.mark.asyncio
async def test_ensure_dependencies_leaves_existing_peer_untouched(
    db_session: AsyncSession,
):
    await _mk_workspace(db_session, "personal")
    await _mk_workspace(db_session, "highway")
    db_session.add(
        models.Peer(
            name="robsherman",
            workspace_name="personal",
            internal_metadata={"card": "SOURCE"},
        )
    )
    db_session.add(
        models.Peer(
            name="robsherman",
            workspace_name="highway",
            internal_metadata={"card": "TARGET"},
        )
    )
    db_session.add(models.Session(name="s1", workspace_name="personal"))
    await db_session.flush()
    db_session.add(
        models.Message(
            session_name="s1",
            workspace_name="personal",
            peer_name="robsherman",
            content="hi",
            seq_in_session=0,
        )
    )
    await db_session.flush()

    from scripts.move_session_workspace import ensure_dependencies

    created_peers, _ = await ensure_dependencies(
        db_session, "personal", "highway", "s1"
    )
    await db_session.flush()

    assert created_peers == []  # already present, not created
    existing = await db_session.scalar(
        select(models.Peer).where(
            models.Peer.workspace_name == "highway",
            models.Peer.name == "robsherman",
        )
    )
    assert existing.internal_metadata == {"card": "TARGET"}  # NOT clobbered


@pytest.mark.asyncio
async def test_relocate_preserves_id_and_moves_children(db_session: AsyncSession):
    await _mk_workspace(db_session, "personal")
    await _mk_workspace(db_session, "highway")
    db_session.add(
        models.Peer(name="robsherman", workspace_name="highway")
    )  # target peer exists
    db_session.add(models.Session(name="s1", workspace_name="personal"))
    await db_session.flush()
    src_sess = await db_session.scalar(
        select(models.Session).where(
            models.Session.workspace_name == "personal",
            models.Session.name == "s1",
        )
    )
    src_id, src_created = src_sess.id, src_sess.created_at
    db_session.add(models.Peer(name="robsherman", workspace_name="personal"))
    await db_session.flush()
    db_session.add(
        models.Message(
            session_name="s1",
            workspace_name="personal",
            peer_name="robsherman",
            content="hi",
            seq_in_session=0,
        )
    )
    await db_session.flush()

    from scripts.move_session_workspace import ensure_dependencies, relocate_in_place

    await ensure_dependencies(db_session, "personal", "highway", "s1")
    await relocate_in_place(db_session, "personal", "highway", "s1", "s1")
    await db_session.flush()

    moved = await db_session.scalar(
        select(models.Session).where(
            models.Session.workspace_name == "highway",
            models.Session.name == "s1",
        )
    )
    assert moved is not None
    assert moved.id == src_id  # id preserved
    assert moved.created_at == src_created
    assert await _count(db_session, models.Message, "highway", "s1") == 1
    assert await _count(db_session, models.Message, "personal", "s1") == 0
    # no orphaned source session row
    assert await _session_row_helper(db_session, "personal", "s1") is None


@pytest.mark.asyncio
async def test_clear_queue_deletes_rows(db_session: AsyncSession):
    await _mk_workspace(db_session, "personal")
    db_session.add(models.Session(name="s1", workspace_name="personal"))
    await db_session.flush()
    # seed a processed queue row for the session
    sess = await db_session.scalar(
        select(models.Session).where(
            models.Session.workspace_name == "personal", models.Session.name == "s1"
        )
    )
    db_session.add(
        models.QueueItem(
            session_id=sess.id,
            workspace_name="personal",
            work_unit_key="test-key-1",
            task_type="representation",
            payload={},
            processed=True,
        )
    )
    await db_session.flush()

    from scripts.move_session_workspace import clear_session_queue

    deleted = await clear_session_queue(db_session, "personal", "s1", force=False)
    await db_session.flush()
    assert deleted == 1


@pytest.mark.asyncio
async def test_clear_queue_raises_on_pending_without_force(db_session: AsyncSession):
    await _mk_workspace(db_session, "personal")
    db_session.add(models.Session(name="s1", workspace_name="personal"))
    await db_session.flush()
    sess = await db_session.scalar(
        select(models.Session).where(
            models.Session.workspace_name == "personal", models.Session.name == "s1"
        )
    )
    db_session.add(
        models.QueueItem(
            session_id=sess.id,
            workspace_name="personal",
            work_unit_key="test-key-2",
            task_type="representation",
            payload={},
            processed=False,
        )
    )
    await db_session.flush()

    from scripts.move_session_workspace import clear_session_queue

    with pytest.raises(MoveError, match="pending queue items"):
        await clear_session_queue(db_session, "personal", "s1", force=False)


@pytest.mark.asyncio
async def test_clear_queue_force_deletes_pending(db_session: AsyncSession):
    await _mk_workspace(db_session, "personal")
    db_session.add(models.Session(name="s1", workspace_name="personal"))
    await db_session.flush()
    sess = await db_session.scalar(
        select(models.Session).where(
            models.Session.workspace_name == "personal", models.Session.name == "s1"
        )
    )
    db_session.add(
        models.QueueItem(
            session_id=sess.id,
            workspace_name="personal",
            work_unit_key="test-key-3",
            task_type="representation",
            payload={},
            processed=False,
        )
    )
    await db_session.flush()

    from scripts.move_session_workspace import clear_session_queue

    deleted = await clear_session_queue(db_session, "personal", "s1", force=True)
    await db_session.flush()
    assert deleted == 1


@pytest.mark.asyncio
async def test_apply_then_integrity_clean(db_session: AsyncSession):
    await _mk_workspace(db_session, "personal")
    await _mk_workspace(db_session, "highway")
    await _mk_session_with_messages(db_session, "personal", "s1", "robsherman", 2)

    from scripts.move_session_workspace import apply_moves, plan_moves

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


def test_build_parser_defaults():
    from scripts.move_session_workspace import build_parser

    args = build_parser().parse_args(
        ["--from", "personal", "--to", "highway", "--session", "s1"]
    )
    assert args.source == "personal" and args.target == "highway"
    assert args.session == ["s1"]
    assert args.apply is False  # dry-run default
    assert args.on_collision == "rename"


@pytest.mark.asyncio
async def test_cross_boundary_premises_flags_only_outside_move_set(
    db_session: AsyncSession,
):
    await _mk_workspace(db_session, "personal")
    db_session.add(models.Peer(name="p", workspace_name="personal"))
    for s in ("a", "b", "other"):
        db_session.add(models.Session(name=s, workspace_name="personal"))
    await db_session.flush()
    # Collection required by Document FK (observer, observed, workspace_name)
    db_session.add(
        models.Collection(observer="p", observed="p", workspace_name="personal")
    )
    await db_session.flush()
    # premise docs
    prem_co = models.Document(
        workspace_name="personal",
        session_name="b",
        observer="p",
        observed="p",
        content="co",
        source_ids=[],
    )
    prem_out = models.Document(
        workspace_name="personal",
        session_name="other",
        observer="p",
        observed="p",
        content="out",
        source_ids=[],
    )
    db_session.add_all([prem_co, prem_out])
    await db_session.flush()
    # a conclusion in session "a" citing both premises
    db_session.add(
        models.Document(
            workspace_name="personal",
            session_name="a",
            observer="p",
            observed="p",
            content="concl",
            source_ids=[prem_co.id, prem_out.id],
        )
    )
    await db_session.flush()

    from scripts.move_session_workspace import cross_boundary_premises

    flagged = await cross_boundary_premises(db_session, "personal", {"a", "b"})
    assert prem_out.id in flagged  # "other" is outside the move set → flagged
    assert prem_co.id not in flagged  # "b" is co-moved → not flagged


@pytest.mark.asyncio
async def test_relocate_create_new_repoints_and_deletes_old(db_session: AsyncSession):
    await _mk_workspace(db_session, "personal")
    await _mk_workspace(db_session, "highway")
    db_session.add(models.Peer(name="robsherman", workspace_name="highway"))
    db_session.add(models.Peer(name="robsherman", workspace_name="personal"))
    db_session.add(models.Session(name="s1", workspace_name="personal"))
    await db_session.flush()
    old = await db_session.scalar(
        select(models.Session).where(
            models.Session.workspace_name == "personal", models.Session.name == "s1"
        )
    )
    old_id = old.id
    db_session.add(
        models.Message(
            session_name="s1",
            workspace_name="personal",
            peer_name="robsherman",
            content="hi",
            seq_in_session=0,
        )
    )
    await db_session.flush()

    from scripts.move_session_workspace import relocate_create_new

    await relocate_create_new(db_session, "personal", "highway", "s1", "s1")
    await db_session.flush()

    moved = await db_session.scalar(
        select(models.Session).where(
            models.Session.workspace_name == "highway", models.Session.name == "s1"
        )
    )
    assert moved is not None and moved.id != old_id  # id churns on fallback
    assert await _count(db_session, models.Message, "highway", "s1") == 1
    assert await _session_row_helper(db_session, "personal", "s1") is None
