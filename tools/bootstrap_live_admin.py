"""One-time local operator bootstrap. Subsequent role changes belong in /live/admin."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live import control_plane as cp, live_service as svc
from live.env_loader import load_private_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "username", help="Existing live username to grant the first administrator role"
    )
    args = parser.parse_args()
    load_private_env()
    svc.ensure_schema()
    with cp.transaction() as c:
        if c.execute(
            "SELECT 1 FROM live_admin_roles LIMIT 1"
        ).fetchone() or os.environ.get("LIVE_ADMIN_USER_IDS"):
            parser.error(
                "An administrator already exists; use the admin UI for additional roles"
            )
        row = c.execute(
            "SELECT user_id,username FROM live_users WHERE lower(username)=?",
            (args.username.strip().lower(),),
        ).fetchone()
        if not row:
            parser.error("Existing user not found")
        c.execute("INSERT INTO live_admin_roles VALUES(?)", (row["user_id"],))
        cp.audit(
            c,
            "local-operator",
            "bootstrap_admin",
            row["user_id"],
            {"username": row["username"]},
        )
    print("Administrator assigned. Sign in and open /live/admin.")


if __name__ == "__main__":
    main()
