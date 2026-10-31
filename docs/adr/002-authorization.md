# ADR-002: Authorization: roles + per-module permissions + object ownership/sharing, enforced in one layer

- **Status:** Proposed
- **Date:** 2026-10-29. Revised for generic self-hosting.
- **Related:** [AUDIT](../AUDIT.md) §2.3 (coupling 2), §3.2 (O1–O7), [ADR-001](001-identity.md), [ARCHITECTURE](../ARCHITECTURE.md) §4

## Context

The audit's worst authorization bugs have one cause: **authorization was a helper that routes had to remember to call, and it was called only for the object in the URL path**.

- Any *other* id in the request (`parent_id`, `new_parent_id`) was trusted. As a result, user B could create folders, upload files and move objects into user A's tree (O1–O3, verified).
- Domain functions took `owner_id: int | None = None`, and `None` meant "no filter" (O6).
- A missing object returned 404, while someone else's returned 403 (O5).

Foundation adds household roles, per-module access, an admin console and, later, sharing. The design must make this bug class **structurally impossible**, not just "fixed this time".

## Decision

### 1. Three layers of permission

| Layer | Question | Stored in | Example |
|---|---|---|---|
| **Role (RBAC)** | What kind of household member is this? | `users.role` ∈ {`owner`,`admin`,`adult`,`child`,`guest`} | Child |
| **Permission (per module)** | May this role/person use this capability? | `role_permissions` (role → permission), plus `user_permission_overrides` (allow/deny for one user) | `drive.files.write`, `admin.users.manage` |
| **Object (ownership/sharing)**, from First modules | May this person act on *this* thing? | `owner_id` on module rows, plus a kernel `shares` table | Alice's "Holiday" folder is shared read-only with the `adult` role |

- **Permissions are strings** of the form `<module>.<resource>.<action>`.
- **Modules declare their permissions** in their manifest ([ARCHITECTURE](../ARCHITECTURE.md) §3), each with **default roles**, for example `drive.files.write: [owner, admin, adult, child]`.
- The admin console edits the role→permission matrix and per-user overrides.
- **Module access** is itself a permission: `<module>.access`. If you don't have it, the module's nav entry is hidden and its routes return 404.
- **Effective permissions** = role grants ∪ user allows − user denies.
- **Built-in invariants** that no setting can change:
  - `owner` always has every permission.
  - At least one active owner must exist. Demoting, disabling or deleting the last owner is refused.
  - Only an owner can create, demote or disable an admin or owner.
  - An admin cannot grant a permission they don't hold themselves.
  - `child` and `guest` can never hold `*.share.public`, `admin.*` or `connectors.*`. That is a hard ceiling per role, not a default.
  - `guest` accounts may have `expires_at`. Once it passes, the account is treated as disabled.

**Object rules (designed now, built with the First modules milestone):**
- The owner of a row can do everything with it.
- Shares grant `read` | `write` | `manage` to a user, a role, or (from First modules) a link token with an expiry.
- Folder shares are inherited by descendants.
- A share can never grant more than the sharer holds.
- Children's shares are limited to inside the household (no link shares).

### 2. Where the checks live, and why nothing can bypass them

All authorization goes through one kernel component, `atlas.kernel.authz`. **Routes do not make authorization decisions.** They declare requirements, and the service layer enforces them.

```
HTTP route ──declares──▶ permission (manifest)      ← checked by the router factory before the handler runs
     │
     ▼
Service command(principal, args)                    ← every id argument is resolved through authz
     │   authz.require(principal, "drive.files.write")
     │   parent = repo.get_for(principal, "write", args.parent_id)   ← NOT repo.get(id)
     ▼
Scoped repository (always filters by principal)     ← there is no unscoped query method in module code
     ▼
PostgreSQL (FKs; optional RLS backstop, First modules)
```

**Rule 1: Every route declares a permission, or says `public=True`.** Modules don't create `APIRouter`s directly. They get one from `atlas.sdk.router(module)`, whose decorators *require* a `permission=` argument (or an explicit `public=True` for things like `/health`). At startup the registry walks every mounted route and **refuses to boot** if any route lacks a declaration. A CI test does the same walk.

**Rule 2: A principal is required, never optional.** Service and repository functions take `principal: Principal` as a positional parameter. There is no `None` and no `owner_id: int | None`. The `Principal` type is constructed only by the auth dependency (or by `system_principal()` for audited jobs), so a handler can't fabricate one from request data.

**Rule 3: Every referenced id is loaded through authz.** That covers ids in the path, the query, the body, and nested ids such as `parent_id`, `new_parent_id`, `share.target_id` and `connector_id`.

- Repositories expose `get_for(principal, action, id)` and `list_for(principal, action, filter)`. Raw `get(id)` exists only inside the kernel.
- The `parent_id` bug becomes a type-level impossibility: to place something under a parent, the service must first obtain the parent via `get_for(principal, "write", parent_id)`, which fails for someone else's folder.
- Domain invariants are checked in the service next to it: the parent is a folder, there are no cycles, and the parent is not trashed.

**Rule 4: Not yours looks the same as not found.** `get_for` raises `NotFound` both when the row doesn't exist and when the principal may not see it. `Forbidden` (403) is used only when the principal *can see* the object but lacks the action (e.g. read-only share → write). That closes O5.

**Rule 5: Module code cannot reach around the kernel.**
- Modules import only from `atlas.sdk` (the stable kernel API). They don't import `atlas.kernel.*` internals, `sqlalchemy.orm.Session` construction, or other modules.
- This is enforced with `import-linter` contracts in CI ([ARCHITECTURE](../ARCHITECTURE.md) §2).
- The session that modules receive is a unit-of-work bound to the principal.

**Rule 6: Admin and step-up permissions are just permissions with a flag.**
- Permissions marked `step_up: true` in a manifest (e.g. `admin.users.manage`, `admin.roles.manage`, `connectors.manage`) also require the session's last strong authentication to be newer than `ATLAS_STEP_UP_MAX_AGE` (default 300 s) ([ADR-001](001-identity.md), [ADR-003](003-internet-exposure.md)).
- If it's older, the API returns `401 step_up_required` and the UI re-runs the OIDC re-auth.

**Rule 7: Decisions are audited.**
- Every allowed admin-scope action, and every denied action, writes an `audit_log` row (actor, action, target, result, request id, client IP) **in the same transaction** as the change.
- Denials are rate-limited in the log, so a scanner can't flood it.

### 3. Database backstop (defense in depth)

- **Foreign keys everywhere** (`owner_id → users.id`, `parent_id → <same table>.id`), plus check constraints for invariants that can be stated in SQL.
- **PostgreSQL Row-Level Security** on module tables that hold user content, starting with the files module (First modules):
  - The unit-of-work sets `SET LOCAL atlas.user_id = …` and `atlas.role = …` per transaction.
  - Policies mirror owner/share visibility.
  - If a future bug skips `get_for`, the DB still returns nothing.
- Foundation's kernel tables (users, roles, sessions, audit) are reached only by kernel code and admin permissions, so RLS is deferred there.

## Alternatives considered

- **Keep router-level helpers (`_get_owned_object`) but be more careful.** Rejected. That is exactly what failed, because it depends on every author remembering every id.
- **A policy engine (OPA/Cedar/Casbin).** Rejected for Foundation: overkill for one household, and it adds a moving part. `authz` keeps a narrow `check(principal, action, resource)` interface, so one could sit behind it later.
- **Permissions only from IdP groups.** Rejected. Fine-grained module permissions and object sharing are Atlas concepts, and changing them must not need an IdP round-trip. The IdP groups Atlas *does* manage are limited to client access (e.g. Immich).
- **RLS as the only mechanism.** Rejected. It's a strong backstop, but it gives poor error semantics and is hard to test alone. It complements the service layer rather than replacing it.

## Verification (tests that must exist)

1. **Route inventory test:** every mounted route has a permission or `public=True`. It fails on any undeclared route.
2. **Two-user matrix test**, for every endpoint taking an id (in any position):
   - user B gets 404 for user A's id;
   - an id of the wrong type (a file used as `parent_id`) is rejected;
   - a non-existent id gives 404.
   This is generated from the route inventory plus OpenAPI parameter metadata, so new endpoints are covered automatically. **The audit's O1–O3 reproduce here as named regression cases.**
3. **Role matrix test:** for each built-in role × each permission default, check the effective decision.
4. **Invariant tests:**
   - last owner can't be removed;
   - an admin can't elevate themselves or modify an owner;
   - child/guest ceilings hold;
   - an expired guest is denied.
5. **Step-up test:** a `step_up` permission with a stale `auth_time` gives `401 step_up_required`; a fresh one gives 200.
6. **Import-linter contract:** modules depend only on `atlas.sdk`.
7. **Audit test:** each admin mutation writes exactly one audit row, and it is written in the same transaction (rollback leaves no row).

## Consequences

- There's a little more ceremony per endpoint (declare a permission, use `get_for`), in exchange for authorization that can't be forgotten.
- Module authors get a small SDK surface. Kernel internals can change without breaking modules.
- Porting today's drive/objects code in First modules is mostly mechanical. Every `get_object(db, id)` becomes `files.get_for(principal, action, id)`.
