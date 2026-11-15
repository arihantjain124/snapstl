"""`uv run snapstl-db …` — shared database (Supabase) tools.

  check        Test the connection from .env (SNAPSTL_DATABASE_URL) and show what's in the database.
  move-local   One time, on the computer that has the orders so far: copy its local database into
               the empty shared database. Its batches stay this computer's batches.
"""
from __future__ import annotations

import sys

from . import computer, db, maintenance


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if not db.shared():
        sys.exit("No shared database set: add SNAPSTL_DATABASE_URL to the .env file in the SnapSTL folder "
                 "(see README → Shared database).")
    try:
        if cmd == "check":
            s = maintenance.stats()
            print(f"Connected to the shared database as “{computer.NAME}”.")
            print(f"Orders: {sum(s['orders'].values())}  Batches: {sum(s['batches'].values())}  "
                  f"Computers with batches: {', '.join(c['name'] for c in s['computers']) or 'none yet'}")
        elif cmd == "move-local":
            if not db.DB_PATH.exists():
                sys.exit(f"No local database on this computer ({db.DB_PATH}) — nothing to move.")
            counts = maintenance.copy_local_to_shared()
            print("Copied to the shared database: " + ", ".join(f"{n} {t}" for t, n in counts.items()))
            print(f"The local database stays at {db.DB_PATH} as a backup.")
        else:
            sys.exit(__doc__)
    except RuntimeError as e:
        sys.exit(str(e))
    except Exception as e:  # noqa: BLE001  (connection problems: show them plainly)
        sys.exit(f"Couldn't use the shared database: {e}")


if __name__ == "__main__":
    main()
