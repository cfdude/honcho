import pytest
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
