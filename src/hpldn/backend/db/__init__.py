"""
Database bootstrap SQL shipped with the package.

``sql/`` holds the secret-free scripts a database needs around the alembic
chain (``db_init.sql``, ``db_verify_init.sql``, ``db_grants_post_migration.sql``,
``db_enable_api_login.sh``) and the fragments they share with the serving
host, whose ``api_role.sql`` and ``grants.sql`` `render_vps_sql` builds.
"""

from pathlib import Path

SQL_DIR = Path(__file__).resolve().parent / "sql"
