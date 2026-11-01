#!/usr/bin/env bash
# =====================================================================
# Nightly: replay the whole migration chain from an empty database.
#
#     scripts/ci/replay_chain.sh            # needs every dataset in migrations/inputs.yaml
#
# The from-empty replay takes hours and needs ~100 GB of source data, so it
# runs here, on its own schedule, rather than in pytest. In an ephemeral
# PostgreSQL 18 cluster (tests/_pg.py: initdb into a temporary directory,
# trust on the loopback, db_init.sql applied with a non-superuser migration
# role) it runs, as bare_metal does:
#
#   alembic upgrade 0001
#   StagingCopy (the out-of-band staging.ukhpucl bulk load)
#   alembic upgrade head
#   db_grants_post_migration.sql, then db_verify_init.sql
#
# and then checks alembic_version is the chain's head and
# production.london_houses has exactly migrations.london_houses_view's
# VIEW_COLUMNS, in order. The cluster is deleted at the end, also on failure;
# set HPLDN_REPLAY_KEEP=1 to keep it (its directory is printed).
# =====================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

uv run python - <<'PY'
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from sqlalchemy import text

from hpldn.backend.common.config import get_cfg
from hpldn.backend.db import SQL_DIR
from hpldn.backend.migrations.chain import CHAIN_HEAD
from hpldn.backend.migrations.london_houses_view import VIEW_COLUMNS
from hpldn.backend.migrations.seed_registry import check_sources
from hpldn.backend.tests._pg import start_cluster, stop_cluster

started = time.monotonic()
check_sources(get_cfg())  # every dataset present, before hours are spent
base = Path(tempfile.mkdtemp(prefix="hpldn-replay-"))
cluster = start_cluster(base)
print(f"replay_chain: cluster in {base} on port {cluster.port}", flush=True)
try:
    cluster.alembic_upgrade("0001")
    with cluster.pinned_db_url():
        from hpldn.backend.preprocessing.staging import StagingCopy

        StagingCopy(copy_type="polars")
    cluster.alembic_upgrade("head")
    common = ["-v", f"migration_role={cluster.migration_role}", "-v", f"api_role={cluster.api_role}"]
    cluster.psql(*common, "-f", str(SQL_DIR / "db_grants_post_migration.sql"), role=cluster.migration_role)
    result = cluster.psql(*common, "-f", str(SQL_DIR / "db_verify_init.sql"))
    if "all checks passed" not in result.stdout:
        sys.exit(f"replay_chain: db_verify_init.sql did not pass:\n{result.stdout}\n{result.stderr}")
    with cluster.migration_engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        columns = [
            row[0]
            for row in connection.execute(text(
                "SELECT attname FROM pg_attribute WHERE attrelid = 'production.london_houses'::regclass "
                "AND attnum > 0 AND NOT attisdropped ORDER BY attnum"))
        ]
    if version != CHAIN_HEAD:
        sys.exit(f"replay_chain: alembic_version is {version}, the chain's head is {CHAIN_HEAD}")
    if columns != list(VIEW_COLUMNS):
        sys.exit(f"replay_chain: production.london_houses columns differ from VIEW_COLUMNS:\n{columns}")
    print(f"replay_chain: {CHAIN_HEAD} from empty in {(time.monotonic() - started) / 3600:.1f} h", flush=True)
except subprocess.CalledProcessError as exc:
    sys.exit(f"replay_chain: {' '.join(map(str, exc.cmd))} failed:\n{exc.stderr}")
finally:
    if os.environ.get("HPLDN_REPLAY_KEEP") == "1":
        print(f"replay_chain: kept the cluster in {base} (pg_ctl -D {cluster.datadir} stop)", flush=True)
    else:
        stop_cluster(cluster)
        subprocess.run(["rm", "-rf", str(base)], check=False)
PY
