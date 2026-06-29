# Lossless cross-workspace session move — design

**Date:** 2026-06-29
**Status:** Approved design → implementation planning
**Repo:** fork of `plastic-labs/honcho` (`cfdude/honcho`), branch `feat/cross-workspace-session-move`
**Conductor epic:** `cross-workspace-session-move` (lane: superpowers)

## Problem

In a multi-workspace Honcho deployment, conversation data can land in the **wrong workspace** ("workspace bleed"). Concretely: Highway (employer) sessions were written into the `personal` workspace when their `HONCHO_WORKSPACE=highway` override wasn't in effect at launch time. The result is Highway-origin sessions (`robsherman-maca`, `robsherman-highway-plugin-marketplace`, `robsherman-barry`) sitting in `personal`, where their derived conclusions get recalled into personal sessions.

Deleting that data is the wrong fix — it belongs to `highway`, not nowhere. Honcho exposes **no native cross-workspace move** (session routes are within-workspace). The only lever today is destructive `delete_conclusion` calls, or hand-written SQL on the memory DB.

Because the deployment is self-hosted (Postgres at `localhost:5532`) and a workspace is just a `workspace_name` column, **lossless reassignment is feasible** — but it is a multi-table, FK-ordered operation with real edge cases, so it deserves a proper, reusable tool rather than ad-hoc SQL.

## Goal

A reusable admin utility an AI agent (or human) can run to **move one or more named sessions from a source workspace to a target workspace, losslessly** — messages, embeddings, and derived conclusions included — without deleting data.

### Non-goals (v1)

- **Peer-global conclusions** (documents with `session_name IS NULL`) — not tied to a session; left for a separate manual review. (In the current contamination, only 39 of ~937 offending docs are peer-global; the rest are session-scoped and fully covered here.)
- **Merge** into an existing same-named target session — collisions are handled by **rename**, not merge (see Decisions).
- **Non-session granularity** (e.g. move an individual message or a peer-pair collection).
- **External vector stores** (turbopuffer / lancedb). v1 targets the default **pgvector** path, where embeddings live in the `message_embeddings` table keyed by `workspace_name`. (A note will flag that external stores need namespace-hash handling.)

## Decisions (approved)

1. **Form:** an admin script in the forked server repo — `scripts/move_session_workspace.py` — using Honcho's own SQLAlchemy models/session (stays correct as the schema evolves). Agent-invocable via `uv run python scripts/move_session_workspace.py …`. Fits the existing `scripts/` pattern (e.g. `generate_jwt.py`).
2. **Collision:** when the target workspace already has a session of that name (`UNIQUE (name, workspace_name)`), **rename** the moved session to a distinct name (default `‹name›-from-‹source›`, counter-suffixed if needed). Both copies preserved; nothing merged or deleted. An `--on-collision abort` mode stops with a report instead.
3. **Derived layer:** **move everything exactly** — relocate messages, `message_embeddings`, and `documents` (conclusions) as-is. Lossless, deterministic, zero LLM/embedding recompute. The tool auto-creates any peers/collections the target workspace is missing and re-points document FKs.

## Relevant schema (verified)

Tables carrying `workspace_name`: `collections`, `documents`, `message_embeddings`, `messages`, `peers`, `queue`, `session_peers`, `sessions`, `webhook_endpoints`. For a **session move** the surface is: `sessions`, `messages`, `message_embeddings`, `documents`, `session_peers`, `queue`, plus the `collections` that documents reference.

Key constraints / relationships:
- `sessions`: PK `id` (global nanoid); **`UNIQUE (name, workspace_name)`** ← the collision constraint.
- Children reference the session by **`(session_name, workspace_name)` composite FK → `sessions(name, workspace_name)`** (not by session `id`), and reference peers by `(peer_name, workspace_name) → peers(name, workspace_name)`.
- `documents` reference a collection by `(observer, observed, workspace_name) → collections(observer, observed, workspace_name)` and are tagged with `session_name` (session-scoped) or `NULL` (peer-global).

Implication: moving a session to the target workspace requires the target to already contain the referenced **peers** and **(observer, observed) collections** — or the tool must create them.

## Design

### CLI

```
uv run python scripts/move_session_workspace.py \
  --from <source_ws> --to <target_ws> \
  --session <name> [--session <name> ...] \
  [--on-collision rename|skip]       # default: rename
  [--rename-suffix "-from-{source}"] # default
  [--apply]                          # omit ⇒ dry-run (default)
  [--no-backup]                      # default: auto pg_dump before --apply
```

### Algorithm (per run, single transaction)

For each requested session (resolved by `(name, source_ws)`):

1. **Validate** — source workspace + session exist; target workspace exists; abort with a clear message otherwise.
2. **Resolve collision** — if `(name, target_ws)` exists, compute a new target name from `--rename-suffix` (counter-suffix on further collision). Under `--on-collision skip`, that session is reported and left in place; other requested sessions still proceed.
3. **Ensure target dependencies** — for every distinct `peer_name` and every distinct `(observer, observed)` collection referenced by the session's messages/documents, create the corresponding row in `target_ws` if absent (minimal copy: identity only).
4. **Relocate, FK-safe** — within the transaction, in an order that keeps every composite FK resolvable at each step (children reference the session by `(name, workspace_name)`, not `id`):
   - create the target `sessions` row (`workspace_name = target`, `name = (renamed?)`),
   - re-point `messages`, `message_embeddings`, `documents` (incl. their collection FK), `session_peers`, and `queue` rows to `(target_name, target_ws)`,
   - remove the now-orphaned source `sessions` row.
   Nothing is duplicated; nothing is deleted — records are **reassigned**. (Implementation verifies whether any table references the session by `id` — e.g. queue/active-queue — and includes those; covered by tests.)

### Output

- **Dry-run (default):** prints, per session — source→target names (incl. rename), message/embedding/document counts, and the peers/collections that would be created. No writes.
- **`--apply`:** runs an auto `pg_dump` of the Honcho DB first (timestamped path, printed), then executes the move in **one transaction** (all-or-nothing; any error rolls back).

## Error handling

- Missing source/target workspace or source session → exit non-zero with a precise message; no writes.
- Collision under `skip` → reported, that session left in place, other requested sessions still proceed.
- Any DB error mid-apply → transaction rollback; the pre-`--apply` `pg_dump` is the backstop.
- Refuse to run if `--from == --to`.

## Testing (TDD, against a disposable/dev Postgres; writes isolated via a test DB or rolled-back transactions)

1. **Clean move** (no collision; e.g. `barry`) — session+messages+docs+embeddings appear under target, gone from source; counts preserved.
2. **Rename on collision** — target already has the name → moved session lands under `‹name›-from-‹source›`; existing target session untouched.
3. **Missing-peer auto-create** — target lacking a referenced peer → peer created; move succeeds.
4. **Missing-collection auto-create** — target lacking a referenced `(observer, observed)` collection → created; documents re-point correctly.
5. **Dry-run writes nothing** — DB byte-identical after a dry-run.
6. **Transaction rollback** — injected error mid-apply leaves the DB unchanged.
7. **Round-trip integrity** — message/document counts and content hashes match pre/post move.

## First real use (the cleanup that motivated this)

Once built + verified, the tool's debut is the actual `personal → highway` cleanup:
- `robsherman-barry` (4 msgs) — clean move (no collision).
- `robsherman-maca` (129 msgs) and `robsherman-highway-plugin-marketplace` (49 msgs) — rename-on-collision (`highway` already holds larger same-named sessions).

(`robsherman-snowflake-mcp-server` stays — it's in `~/Servers`, classified personal.)

## Risks / open items

- **Session `id` references.** Children FK by `(name, workspace_name)`, but `queue`/`active_queue_sessions` may reference session `id`; the implementation must enumerate and handle any id-based refs (test-covered).
- **pgvector only.** External vector stores namespace embeddings by a workspace-derived hash; v1 documents this limitation rather than handling it.
- **Recall cache.** After a move, the plugin's recall cache may briefly surface stale conclusions until a session restart; note in the runbook.

## Upstream

After verification, PR `scripts/move_session_workspace.py` + tests to `plastic-labs/honcho` — a general "fix workspace bleed without data loss" capability useful to any multi-workspace self-hoster. (Local conductor/PM files are excluded from that PR.)
