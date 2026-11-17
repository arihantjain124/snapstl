"""`uv run snapstl-db …` — the shared order records (Supabase) from a terminal.

  check   Test the connection set in Settings → Shared order records and show what the records hold.
  sync    Push this computer's changes and load everyone's (same as Settings → Connections → Sync now).
  export  Save a copy of every shared record in this computer's backups folder (JSON).
  schema  Print the SQL that creates the shared records (no connection needed; docs/supabase-schema.sql).
"""
from __future__ import annotations

import sys
from datetime import datetime

from . import computer, sync


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    try:  # Windows consoles default to cp1252 and CRLF: the schema has "→" and docs/supabase-schema.sql is UTF-8, LF
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    except (AttributeError, ValueError):
        pass
    if cmd == "schema":
        print(sync.schema_sql(), end="")
        return
    if not sync.enabled():
        sys.exit("No shared order records set up: connect them in the app (Settings → Connections → Shared order records).")
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
        elif cmd == "export":
            r = sync.export()
            print(f"Saved {r['orders']} order record(s) and {r['batches']} batch record(s) to {r['file']}")
        else:
            sys.exit(__doc__)
    except sync.SyncError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
