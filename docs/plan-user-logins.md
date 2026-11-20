# Plan: user logins and roles (deferred)

Status: **planned, not built.** Discussed with the owner on 2026-11-19 / 2026-11-20 and put on hold by the
owner ("skip the login update for now"). Build it only when the owner asks. Until then the app on each
computer opens without login on 127.0.0.1, and the logistics page (port 8002) keeps its one shared password.

## What the owner asked for

- Each person has their own login; what they see and may do depends on their role.
- Roles for now: **Admin** and **Production** only. The logistics page stays as it is (its own password).
- Only Admin uploads reports. Any user may create batches.
- Print → Verify: no "Add name"; every check (and every print started) is recorded under the signed-in
  person, so each person is responsible for the names and bases checked in their shift.

## Proposed access (still to confirm with the owner)

| Area | Production | Admin |
|---|---|---|
| Orders | see, select, create batches | also upload reports |
| Customer phone and address | hidden (open question) | shown |
| Check name first: correct a name | open question | yes |
| Batches, Print, Verify, Printers, Activities | full use | full use |
| Slicing | see profiles (open question: view only?) | change profiles |
| Settings (connections, updates, clean-up, reset, users) | no access | full |
| Users | change own password | add / remove people, roles, reset passwords |

Enforced on the server, per API route, not only by hiding buttons. **Anything not listed is Admin only**, so
a new route can't be opened to Production by mistake.

## How not to break the running system

1. The SnapSTL Helper (MakerWorld tab) calls `/api/helper/*` without a login — keep it exempt; add a test.
2. Background work (sync, Bambuddy worker, batch builder, slicing, print tracking) doesn't pass through the
   login and must stay that way.
3. Lockout: a command on the farm computer resets the admin password (extend `snapstl-admin`).
4. Users live in a **new** shared-records table, copied to each computer, so sign-in works while Supabase or
   the internet is down. No existing table changes, so computers don't all have to update the same day; an
   older computer just stays open without login until it updates.
5. Shared farm computer: a **Switch user** button and sign-out by itself after 12 hours, so a shift's checks
   aren't saved under the previous person's name.
6. Verify keeps storing the person's name in `plate_checks.staff` (text) — the login name fills it, so old
   checks ("Before 0.24", names added with Add name) stay valid.
7. Bambuddy (port 8000) stays open to the local network; SnapSTL logins don't protect it, and Bambuddy is
   never modified.
