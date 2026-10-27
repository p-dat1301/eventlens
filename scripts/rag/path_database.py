"""Create an empty sibling test database and print its connection string.

Usage: ``path_database.py NAME`` with ``EVENTLENS_RAG_DATABASE_URL`` pointing at
an explicitly owned, isolated test server. Creates a uniquely named empty
database without deleting any existing database. The adopted path therefore
runs on a public schema built by the historical runner, never on rows left by
the fresh path. Refuses names outside ``[a-z0-9_]``.
"""

import os
import re
import sys
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo


def main() -> None:
    """Create a unique test database and print its conninfo on stdout."""
    if os.environ.get("EVENTLENS_RAG_TEST_SERVER_OWNED") != "1":
        sys.exit("refusing database creation outside an owned test server")
    name = sys.argv[1]
    if not re.fullmatch(r"[a-z0-9_]+", name):
        sys.exit(f"invalid database suffix: {name!r}")
    base = os.environ["EVENTLENS_RAG_DATABASE_URL"]
    target = f"{conninfo_to_dict(base).get('dbname', 'postgres')}_{name}_{uuid4().hex}"
    with psycopg.connect(base, autocommit=True) as connection:
        _ = connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target))
        )
    sys.stdout.write(make_conninfo(base, dbname=target) + "\n")


if __name__ == "__main__":
    main()
