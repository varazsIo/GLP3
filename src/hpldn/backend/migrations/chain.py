"""Constants and helpers shared by every revision in the chain."""

import importlib.util
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Dict

# A database populated before this chain existed is stamped to BASELINE_HEAD,
# never replayed; the data-repair revisions after it (0017-0020), the search index (0021-0022, 0024),
# the borough rents (0023, replaced by the London rents in 0026), the price
# history and certificate fallback (0025), sale linking (0027-0029), address
# units and certified homes (0030), unit-level search (0031), CPIH
# inflation (0032) and Ofsted school inspections and features (0033-0034) run
# on both databases.
BASELINE_HEAD = "0016"

CHAIN_HEAD = "0034"

# Head of the earlier 84-revision chain this one consolidates.
OLD_CHAIN_HEAD = "0084"

NO_DOWNGRADE = (
    f"Revisions 0002-{CHAIN_HEAD} of this chain do not support downgrade().\n"
    "\n"
    "This is not an oversight. The chain is a seeding pipeline: its "
    "revisions load and derive ~1.9M transactions, 41.6M UPRN coordinates "
    "and 4.6M EPC certificates, and a downgrade past a table-building "
    "revision destroys that data with nothing to restore it from. The old "
    "84-revision chain shipped best-effort inverses that read as though "
    "they worked -- every revision from 0064 onward documented its own as "
    "best-effort, and downgrading past a view-rebuild revision dropped "
    "production.london_houses outright, with no later revision able to "
    "rebuild it.\n"
    "\n"
    "Refusing loudly is the honest behaviour. To go back:\n"
    "  * restore from the pg_dump the cutover runbook takes first; or\n"
    "  * rebuild from empty:\n"
    "        sudo -u postgres psql -v ON_ERROR_STOP=1 -d postgres \\\n"
    "             -f src/hpldn/backend/db/sql/db_init.sql\n"
    "        python -m hpldn.backend.scripts.bare_metal --load-raw\n"
    "        psql -f src/hpldn/backend/db/sql/db_grants_post_migration.sql\n"
)


def no_downgrade():
    """
    Refuse a downgrade; called by every revision from 0002 to `CHAIN_HEAD`.

    Raises
    ------
    NotImplementedError
        Always, with `NO_DOWNGRADE` as the message.
    """
    raise NotImplementedError(NO_DOWNGRADE)


_VERSIONS_DIR = Path(__file__).resolve().parent / "versions"


def load_revision(revision_id: str):
    """
    Import a revision module from ``versions/`` by its id.

    ``versions/`` is loaded by path, not as a package, so a later revision that
    re-runs an earlier revision's step (0017-0020 rebuild what 0006-0016 built)
    imports it through here instead of duplicating its SQL.

    Parameters
    ----------
    revision_id : str
        Four-digit revision id, for example ``"0011"``.

    Returns
    -------
    module
        The revision module. Its ``upgrade()`` is not called.

    Raises
    ------
    LookupError
        If no single file in ``versions/`` starts with `revision_id`.
    """
    matches = sorted(_VERSIONS_DIR.glob(f"{revision_id}_*.py"))
    if len(matches) != 1:
        raise LookupError(
            f"expected one versions/{revision_id}_*.py, found {len(matches)}"
        )
    spec = importlib.util.spec_from_file_location(
        f"hpldn_backend_revision_{revision_id}", matches[0]
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_concurrently(tasks: Dict[str, Callable[[], object]], log=None) -> Dict[str, object]:
    """
    Run independent migration steps at once, each on its own connections.

    PostgreSQL caps parallel query at ``max_parallel_workers`` for the whole
    cluster (8 here), so one statement at a time leaves most of a 24-core
    host idle; separate backends each get a core. Only pass steps that
    write disjoint tables and never ANALYZE, index or drop the same relation:
    two backends ANALYZE-ing one table can fail with "tuple concurrently
    updated".

    Parameters
    ----------
    tasks : dict of str to callable
        Step name to a no-argument callable. Every callable is started.
    log : logging.Logger, optional
        Logger for start, finish and failure lines.

    Returns
    -------
    dict of str to object
        Each step's return value.

    Raises
    ------
    Exception
        The first failure, once every step has finished (a running statement
        cannot be cancelled from another thread, so failing early would leave
        it running unobserved).
    """
    results: Dict[str, object] = {}
    errors = []

    def timed(name, task):
        started = time.monotonic()
        if log is not None:
            log.info("START  %s (concurrent)", name)
        value = task()
        if log is not None:
            log.info("FINISH %s in %.1fs", name, time.monotonic() - started)
        return value

    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = {pool.submit(timed, name, task): name for name, task in tasks.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as exc:  # re-raised below, after the rest finish
                if log is not None:
                    log.error("FAILED %s: %s", name, exc)
                errors.append(exc)
    if errors:
        raise errors[0]
    return results


def vacuum_analyze(engine, tables, log=None) -> None:
    """
    ``VACUUM (ANALYZE)`` `tables` on an autocommit connection.

    For tables a revision just rewrote in bulk: the dead tuples would
    otherwise sit in the indexes the next build's as-of lookups scan until
    autovacuum reaches them. VACUUM cannot run inside a transaction.
    """
    from sqlalchemy import text

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        for table in tables:
            started = time.monotonic()
            connection.execute(text(f"VACUUM (ANALYZE) {table}"))
            if log is not None:
                log.info("VACUUM (ANALYZE) %s in %.1fs", table, time.monotonic() - started)
