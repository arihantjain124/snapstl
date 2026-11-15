"""`uv run snapstl-db …` — the shared order records (Supabase) from a terminal.

  check   Test the connection set in .env (SNAPSTL_DATABASE_URL) and show what the records hold.
  sync    Push this computer's changes and load everyone's (same as Settings → Database → Sync now).
"""
from __future__ import annotations

import sys
from datetime import datetime

from . import computer, sync


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if not sync.enabled():
        sys.exit("No shared records set up: add SNAPSTL_DATABASE_URL to the .env file in the SnapSTL folder "
                 "(README → Shared order records).")
    try:
        if cmd == "check":
            c = sync.check()
            print(f"Connected to the shared order records as “{computer.NAME}”.")
            print("Orders: " + (", ".join(f"{n} {s}" for s, n in sorted(c["orders"].items())) or "none yet"))
            print("Batches by computer: " + (", ".join(f"{k}: {n}" for k, n in c["batches"].items()) or "none yet"))
        elif cmd == "sync":
            s = sync.sync_now()
            if s["error"]:
                sys.exit(s["error"])
            print(f"Synced at {datetime.fromtimestamp(s['last_ok']):%H:%M:%S}: sent {s['pushed']} change(s), "
                  f"loaded {s['pulled']} record(s); {s['pending']} waiting.")
        else:
            sys.exit(__doc__)
    except sync.SyncError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
