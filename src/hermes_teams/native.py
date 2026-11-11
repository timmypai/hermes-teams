"""Thin native adapter; owns no database, transaction, lifecycle or dispatcher."""
from pathlib import Path
from hermes_teams.policy import select_policy


def bound_policy(conn):
    from hermes_teams.site_policy import BINDINGS
    rows = conn.execute('PRAGMA database_list').fetchall()
    main = next((row[2] for row in rows if row[1] == 'main'), '')
    return select_policy(BINDINGS, str(Path(main).resolve()) if main else '')
