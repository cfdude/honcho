# Lossless cross-workspace session move — design

**Date:** 2026-06-29
**Status:** Approved design, revised after Review Gate 1 → implementation planning
**Repo:** fork of `plastic-labs/honcho` (`cfdude/honcho`), branch `feat/cross-workspace-session-move`
**Conductor epic:** `cross-workspace-session-move` (lane: superpowers)

## Problem

In a multi-workspace Honcho deployment, conversation data can land in the **wrong workspace** ("workspace bleed"). Concretely: Highway (employer) sessions were written into the `personal` workspace when their `HONCHO_WORKSPACE=highway` override wasn't in effect at launch time. The result is Highway-origin sessions (`robsherman-maca`, `robsherman-highway-plugin-marketplace`, `robsherman-barry`) sitting in `personal`, where their derived conclusions get recalled into personal sessions.

Deleting that data is the wrong fix — it belongs to `highway`, not nowhere. Honcho exposes **no native cross-workspace move** (session routes are within-workspace). The only lever today is destructive `delete_conclusion` calls, or hand-written SQL on the memory DB.

Because the deployment is self-hosted (Postgres at `localhost:5532`) and a workspace is just a `workspace_name` column, **lossless reassignment is feasible** — but it is a multi-table, FK-ordered operation with real edge cases, so it deserves a proper, reusable tool rather than ad-hoc SQL.

## Goal

A reusable admin utility an AI agent (or human) can run to **move one or more named sessions from a source workspace to a target workspace, losslessly** — messages, embeddings, and derived conclusions included, with every persisted column and identity (`id`, `public_id`, `created_at`) preserved — without deleting data.

**Definition of "lossless" (made explicit per Gate 1 / C1):** after a move, every moved row retains all non-derived columns and its identity. Nothing is recreated-with-defaults. Specifically: the session keeps its `id`, `created_at`, `is_active`, `configuration`, `h_metadata`, `internal_metadata`; messages/embeddings/documents keep their `id`/`public_id`; auto-created target peers/collections are full-column copies (not identity-only).

### Non-goals (v1)

- **Peer-global conclusions** (documents with `session_name IS NULL`) — not tied to a session; left for a separate manual review. (In the current contamination, only 39 of ~937 offending docs are peer-global; the rest are session-scoped and fully covered here.) **Caveat (Gate 1 / I2):** a moved session-scoped conclusion's `Document.source_ids` premises can point at peer-global docs or docs in a non-moved session; those references become dangling after the move (JSONB, no FK → silent). v1 **detects and reports** such cross-boundary premises in the dry-run rather than fixing them.
- **Merge** into an existing same-named target session — collisions are handled by **rename**, not merge.
- **Non-session granularity** (e.g. move an individual message or a peer-pair collection).
- **External vector stores** (turbopuffer / lancedb). v1 targets the default **pgvector** path. (Gate 1 / M3 confirmed: `Document.embedding` is an inline `Vector` column that travels with its row, and `message_embeddings` moves via its `workspace_name` + composite repoint with `message_id → messages.public_id` stable — **there is no workspace-derived namespace/hash in pgvector mode**, so moving the rows is sufficient. External stores namespace by a workspace-derived hash and are out of scope.)

## Decisions (approved; revised post-Gate-1)

1. **Form:** an admin script in the forked server repo — `scripts/move_session_workspace.py` — using Honcho's own SQLAlchemy models/session. Agent-invocable via `uv run python scripts/move_session_workspace.py …`. Fits the existing `scripts/` pattern (e.g. `generate_jwt.py`).
2. **Collision:** when the target already has a session of that name (`UNIQUE (name, workspace_name)`), **rename** the moved session (default `‹name›-from-‹source›`, counter-suffixed if needed). `--on-collision skip` reports and leaves that session in place while others proceed.
3. **Derived layer — move everything exactly, in place.** Relocate `messages`, `message_embeddings`, and `documents` as-is, preserving every column and `public_id`. The session row is **updated in place (id preserved)**, not recreated. The tool auto-creates any missing target peers/collections as **full-column copies** and re-points document collection FKs. **Zero recompute.**

## Relevant schema (verified against `src/models.py`)

Move surface for a session: `sessions`, `messages`, `message_embeddings`, `documents`, `session_peers`, plus the referenced `peers` and `(observer, observed)` `collections`. (Gate 1 confirmed this surface is complete — no other session-referencing table.)

Reference shapes (decisive for the algorithm):
- `sessions`: PK `id` (global nanoid); **`UNIQUE (name, workspace_name)`** — the collision constraint. Carries `is_active`, `created_at`, `configuration`, `h_metadata`, `internal_metadata`.
- `messages`, `message_embeddings`, `documents`, `session_peers`: reference the session by the **`(session_name, workspace_name)` composite FK → `sessions(name, workspace_name)`** (NO ACTION; not deferrable; no `ON UPDATE CASCADE`), and peers by `(peer_name, workspace_name)`.
- `message_embeddings.message_id → messages.public_id` is **`ON DELETE CASCADE`** → children must be **UPDATEd in place, never delete-recreated**.
- `documents` → collections by `(observer, observed, workspace_name)`; `Document.source_ids` (JSONB) holds premise document ids (no FK).
- `queue`: references the session by **`id`** (`session_id`, NO ACTION), with a nullable `workspace_name`, a JSONB `payload` (stores the session nanoid), and a `work_unit_key` string encoding `…:{workspace_name}:{session_name}:…`. It is **transient work-state**, not lossless-relevant.
- `peers` and `collections`: carry `configuration`/`h_metadata`/`internal_metadata`. **Peer cards are not a table** — `set_peer_card` stores them in the observer peer's `internal_metadata`; so a target peer that must be created should be a full-column copy (or the peer-card omission documented).
- `active_queue_sessions`: **no session FK** — only a unique `work_unit_key` string; stale rows are reaped by the reconciler. Nothing to move.

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

1. **Validate** — source workspace + session exist; target workspace exists; `--from ≠ --to`. Abort with a precise message otherwise (no writes).
2. **Resolve collision** — if `(name, target_ws)` exists, compute the renamed target name. Under `--on-collision skip`, report and leave in place; others still proceed.
3. **Ensure target dependencies (full-column copy)** — derive the peer set from the **union** of every peer referenced across the moved tables: `messages` (`peer_name`), `session_peers`, `documents` (`observer`/`observed`), and `collections` (`observer`/`observed`). For each such peer, and each `(observer, observed)` collection referenced by the session's documents, create the row in `target_ws` **only if absent**, copying all columns. (If the peer/collection already exists in target — the common case, e.g. `robsherman`/`claude` — leave it untouched: do not clobber its `internal_metadata`/peer cards with the source copy.)
4. **Relocate, preserving identity** — the composite FKs target `(name, workspace_name)`, have no `ON UPDATE CASCADE`, and are NOT DEFERRABLE, so an in-place change of the session's `(name, workspace_name)` while children still reference the old pair would violate them mid-statement. Within the transaction, suspend that check while both sides are updated, then restore it:
   - **Primary (id-preserving; owner privilege):** make the affected composite FKs deferrable for the transaction (`ALTER TABLE … ALTER CONSTRAINT … DEFERRABLE`, then `SET CONSTRAINTS ALL DEFERRED`), UPDATE the session + children, then restore them to NOT DEFERRABLE before commit (the now-consistent data re-validates). `ALTER TABLE` is transactional, so a rollback reverts the constraint change too. *Equivalent on superuser roles:* `ALTER TABLE … DISABLE TRIGGER ALL` on `sessions` **and every child table**, re-enabled before commit — the default docker `postgres` role is superuser, so `DISABLE TRIGGER` is also available there. The mechanism is chosen by privilege detection (`rolsuper` / can-defer); both take a brief `ACCESS EXCLUSIVE` lock on the affected tables (`sessions`, `messages`, `message_embeddings`, `documents`, `session_peers`), so run during quiescence.
   - **UPDATE in place**: set the `sessions` row's `workspace_name` (and `name` if renamed) — `id`, `created_at`, config/metadata preserved; set `(session_name, workspace_name)` on `messages`, `message_embeddings`, `documents` (and `documents.workspace_name` for the collection FK), and `session_peers` — `public_id`/`id` preserved, no recreation, no CASCADE.
   - **Fallback (no defer/disable privilege; id churns):** create a new `sessions` row (full-column copy, new `id`, target ws/name) → **clear the session's queue rows (step 5) BEFORE deleting the old session** (`queue.session_id` is NO ACTION → an undeleted queue row blocks the old-session delete) → repoint children → delete the old `sessions` row.
   - Restore constraints/triggers, then run a **post-move integrity assertion** (no child with an unparented `(session_name, workspace_name)`; no CASCADE-orphaned embeddings).
5. **Queue** — never repoint (the `work_unit_key`/`payload` embed the old `{workspace_name}:{session_name}` / session nanoid). Treat queue as transient: **assert no pending work for the session, else DELETE its `queue` rows**. In the primary path the session row is never deleted so ordering is free; in the fallback, queue-clear MUST precede the old-session delete (see step 4).
6. **Cross-boundary premise report** — scan moved documents' `source_ids`; report any premise that is peer-global (`session_name IS NULL`) or lives in a non-moved session, so the operator knows which reasoning chains will dangle.

### Output

- **Dry-run (default):** per session — source→target names (incl. rename), message/embedding/document counts, peers/collections to be created, queue rows to be cleared, and the cross-boundary `source_ids` report. No writes.
- **`--apply`:** auto `pg_dump` of the Honcho DB first (timestamped path, printed), then the move in **one transaction** (all-or-nothing).

## Error handling

- Missing source/target workspace or session, or `--from == --to` → exit non-zero, no writes.
- Collision under `skip` → reported, that session left in place, others proceed.
- Pending queue work for a session under `--apply` → refuse (or `--force-clear-queue` to delete) — never repoint.
- Any DB error mid-apply → transaction rollback; the pre-`--apply` `pg_dump` is the backstop.

## Testing (TDD; isolated test DB or rolled-back transactions)

1. **Clean move** (no collision, e.g. `barry`) — session+messages+docs+embeddings under target, gone from source; counts preserved.
2. **Rename on collision** — moved session lands under `‹name›-from-‹source›`; the existing target session untouched.
3. **Missing-peer full-copy** — target lacking a referenced peer → peer created with all columns (incl. `internal_metadata`); move succeeds.
4. **Missing-collection full-copy** — collection created with metadata; documents re-point correctly.
5. **Dry-run writes nothing** — DB byte-identical after a dry-run.
6. **Transaction rollback** — injected error mid-apply leaves the DB unchanged.
7. **Identity & round-trip integrity** — session `id`, `created_at`, message `public_id`s, and message/document **content hashes** match pre/post; (Gate 1 / I4) **FK-orphan assertion** (no unparented children; no `queue.session_id` without a session; no CASCADE-orphaned embeddings).
8. **Queue handling** — a session with pending queue rows: default refuses; `--force-clear-queue` deletes them and the move succeeds; no stale `work_unit_key` survives.
9. **`source_ids` reasoning chain** — a deductive conclusion whose premise is peer-global/another *non-moved* session → the dry-run **reports** the cross-boundary premise; **and** a premise in another session that **is in the same move set** is NOT flagged ("moved" = the whole run's set).
10. **Multi-session single run** — moving several sessions in one invocation is atomic (all-or-nothing).
11. **Peer-card preservation** — when a target peer is created, its `internal_metadata` (peer cards) is copied (asserts C1 is fixed).
12. **Existing target peer untouched** — when the referenced peer already exists in target, its `internal_metadata`/config is NOT overwritten by the source copy.
13. **Fallback (create-new-row) path** — force the no-privilege path: id changes, children repoint correctly, and **queue rows are cleared before the old-session delete** (regression guard for the FK-ordering hazard); same integrity assertions as test 7 plus a check that no row still references the old session `id`.

## First real use (the cleanup that motivated this)

Once built + verified, the tool's debut is the actual `personal → highway` cleanup:
- `robsherman-barry` (4 msgs) — clean move.
- `robsherman-maca` (129 msgs) and `robsherman-highway-plugin-marketplace` (49 msgs) — rename-on-collision (`highway` already holds larger same-named sessions).

(`robsherman-snowflake-mcp-server` stays — it's in `~/Servers`, classified personal.)

## Upstream

After verification, PR `scripts/move_session_workspace.py` + tests to `plastic-labs/honcho` — a general "fix workspace bleed without data loss" capability useful to any multi-workspace self-hoster. Portability across deployment roles: an **owner-privilege** deferrable-constraint primary path (id-preserving), a superuser `DISABLE TRIGGER` equivalent, and a **no-privilege** create-new-row fallback. (Local conductor/PM files are excluded from that PR.)
