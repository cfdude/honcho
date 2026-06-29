import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scripts.move_session_workspace import MoveError, plan_moves
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
