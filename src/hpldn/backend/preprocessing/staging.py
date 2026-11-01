"""Bulk-copy the UCL UK House Prices CSV into ``staging.ukhpucl``."""

import glob
import os
import logging
import subprocess
import threading
import time
import io
import polars as pl
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict
from sqlalchemy import URL, Integer, Float, text
from hpldn.backend.models.staging import UKHPUCL, AppBase
from hpldn.backend.common.config import get_cfg
from hpldn.backend.common.utils import setup_logger
from hpldn.backend.migrations import inputs
from hpldn.backend.common.db import (
    get_db_url,
    get_psql_env,
    get_psql_url,
    get_sql_dev_engine,
    get_force_null_str,
)
from hpldn.backend.common.db import create_db
from hpldn.backend.common.tuning import (
    compute_postgres_settings,
    describe,
    detect_host_resources,
    get_bulk_copy_env,
    get_copy_jobs,
    get_seeding_settings,
    policy_from_cfg,
)
from typing import Union, Iterator


class StagingCopy:
    """
    Copy the UCL UK House Prices CSV into ``staging.ukhpucl``.

    Parameters
    ----------
    input_csv_fp : pathlib.Path, optional
        Source CSV; the ``ukhpucl_csv_fp`` migration input by default.
    copy_type : {"chunked", "polars", "nuke"}, optional
        Copy method; ``"nuke"`` (single pass) is not implemented.
    resolve_mode : {"smart", "nuke"}, optional
        ``"smart"`` skips chunking for a CSV smaller than ``csv_chunk_size``
        and deletes chunk files after loading.
    skip_chunking : bool, optional
        Stored on the instance; chunking is decided by `resolve_mode`.
    log : logging.Logger, optional
        Logger for progress messages.
    """

    def __init__(
        self,
        input_csv_fp: Path = None,
        copy_type: str = "chunked",
        resolve_mode: str = "smart",
        skip_chunking: bool = False,
        log: Union[None, logging.Logger] = None,
    ) -> None:
        self.single_commit = False
        self.input_csv_fp = input_csv_fp
        self.skip_chunking = skip_chunking

        if resolve_mode not in ("smart", "nuke"):
            if log is not None:
                log.warning(
                    "Unsupported resolve mode, defaulting to resolve_mode='smart'"
                )
            self.resolve_mode = "smart"
        else:
            self.resolve_mode = resolve_mode

        if copy_type == "chunked" and resolve_mode == "smart":
            self._chunked_copy(log)
        elif copy_type == "nuke":
            self._nuke_copy(log)
        elif copy_type == "polars":
            self._copy_with_polars(log)
        else:
            raise NotImplementedError(
                "Only copy_type 'chunked' is currently supported."
            )

    def _prepare_files(self, cfg: Dict, log: logging.Logger) -> Path:
        """
        Split the source CSV into chunk files for a chunked copy.

        Parameters
        ----------
        cfg : dict
            Configuration for chunking.
        log : logging.Logger
            Logger for progress messages.

        Returns
        -------
        pathlib.Path
            Directory holding the chunk files.
        """
        staging_csv_fp = inputs.resolve("ukhpucl_csv_fp", cfg).resolve()
        if self.input_csv_fp is not None:
            staging_csv_fp = self.input_csv_fp
        skip_chunking = False
        if self.resolve_mode == "smart":
            ccs = cfg["csv_chunk_size"]
            ccsb = 0
            if ccs[-1] == "G":
                ccsb = 1024**3 * int(ccs[:-1])
            elif ccs[-1] == "M":
                ccsb = 1024**2 * int(ccs[:-1])
            else:
                raise ValueError(
                    "csv_chunk_size must end in 'G' or 'M' (only file size in G and M supported)"
                )
            if os.path.getsize(staging_csv_fp) < ccsb:
                skip_chunking = True
                log.debug("resolve_mode=smart activated, skipping chunking")

        work_dir = Path(staging_csv_fp).parent

        if not skip_chunking:
            headers_fp = inputs.resolve("ukhpucl_csv_headers_fp", cfg).resolve()
            work_dir = headers_fp.parent.joinpath("data_part_")
            log.debug("Preparing files...")
            os.makedirs(work_dir, exist_ok=True)
            subprocess.run(
                f'head -n 1 "{staging_csv_fp}" > "{headers_fp}"', shell=True, check=True
            )
            m = cfg["csv_chunk_size"]
            subprocess.run(
                f'tail -n +2 "{staging_csv_fp}" | gsplit -C {m} -d - "{work_dir}/data_part_"',
                shell=True,
                check=True,
            )
            for f in glob.glob(f"{work_dir}/data_part_*"):
                tmp_fp = Path(f)
                new_fp = tmp_fp.parent.joinpath(
                    tmp_fp.stem.replace("data_part_", "final_data_part_") + ".csv"
                )
                target = str(new_fp)

                if "final_" not in f:
                    subprocess.run(
                        f'cat "{headers_fp}" "{f}" > "{target}" && rm "{f}"',
                        shell=True,
                        check=True,
                    )
            log.debug("Successfully prepared all files!")
        else:
            file_id = 0
            cp_fp = Path(staging_csv_fp).parent.joinpath(
                f"final_data_part_0{file_id}.csv"
            )
            while cp_fp.exists():
                file_id += 1
                if file_id < 10:
                    cp_fp = Path(staging_csv_fp).parent.joinpath(
                        f"final_data_part_0{file_id}.csv"
                    )
                else:
                    cp_fp = Path(staging_csv_fp).parent.joinpath(
                        f"final_data_part_{file_id}.csv"
                    )
            subprocess.run(f'cp "{staging_csv_fp}" "{cp_fp}"', shell=True, check=True)

        return work_dir

    def _set_postgres_variables(
        self, db_url: URL, cfg: Dict, log: logging.Logger
    ) -> None:
        """
        Prepare PostgreSQL and ``staging.ukhpucl`` for a high-speed bulk load.

        WAL and checkpoint sizes are derived from the host through
        `hpldn.backend.common.tuning`; a ``psql:`` block in ``staging.yaml``
        overrides individual keys. The table is set UNLOGGED for the load, and
        ``bare_metal --finalize`` or ``create_ukhpucl_indexes`` sets it back.

        Parameters
        ----------
        db_url : sqlalchemy.URL
            Target database.
        cfg : dict
            Copy configuration.
        log : logging.Logger
            Logger for progress messages.
        """
        log.debug("Setting PostgreSQL variables...")
        psql_url = get_psql_url(db_url)
        psql_env = get_psql_env(db_url)
        resources = detect_host_resources(log)
        policy = policy_from_cfg(cfg)
        derived = compute_postgres_settings(
            resources, policy, cfg.get("data_volume_size_gb")
        )
        seeding = get_seeding_settings(
            resources, policy, cfg.get("data_volume_size_gb")
        )
        overrides = cfg.get("psql") or {}
        log.info("Bulk load tuning for %s", describe(resources))

        def setting(name: str, default: str) -> str:
            """Return the ``psql:`` override for `name`, else `default`."""
            return str(overrides.get(name, default))

        setup_commands = [
            f"SET maintenance_work_mem = '{setting('maintenance_work_mem', derived['maintenance_work_mem'])}';",
            f"SET synchronous_commit = {setting('synchronous_commit', seeding['synchronous_commit'])};",
            f"ALTER SYSTEM SET max_wal_size = '{setting('max_wal_size', seeding['max_wal_size'])}';",
            f"ALTER SYSTEM SET checkpoint_timeout = '{setting('checkpoint_timeout', seeding['checkpoint_timeout'])}';",
            f"ALTER TABLE {AppBase.metadata.schema}.ukhpucl SET (autovacuum_enabled = '{setting('autovacuum_enabled', 'false')}');",
            f"ALTER TABLE {AppBase.metadata.schema}.ukhpucl SET UNLOGGED;",
            "SELECT pg_reload_conf();",
        ]
        for cmd in setup_commands:
            subprocess.run(["psql", psql_url, "-c", cmd], check=True, env=psql_env)
        log.debug("Successfully set PostgreSQL variables!")

    def _copy_chunked_data_to_db(
        self, directory_path: Path, db_url: URL, log: logging.Logger, jobs: int = None
    ) -> None:
        r"""
        ``\copy`` chunk files into the database concurrently.

        Each chunk is independent and commits in its own transaction, so
        several run at once, sized by `hpldn.backend.common.tuning.get_copy_jobs`.

        Parameters
        ----------
        directory_path : pathlib.Path
            Directory of chunk files.
        db_url : sqlalchemy.URL
            Target database.
        log : logging.Logger
            Logger for progress messages.
        jobs : int, optional
            Concurrent ``\copy`` processes; derived from the core count when None.
        """
        psql_url = get_psql_url(db_url)
        force_null_str = get_force_null_str()
        filtered_columns = list()
        for c in UKHPUCL.__table__.columns:
            if c.name != "id" and str(c.name).isupper():
                filtered_columns.append(f'"{c.name}"')
            elif c.name != "id":
                filtered_columns.append(f"{c.name}")
        selected_columns = "(" + ",".join(filtered_columns) + ")"

        files = sorted(glob.glob(os.path.join(directory_path, "final_data_part_*")))
        if jobs is None:
            jobs = get_copy_jobs(log=log)
        jobs = max(1, min(jobs, len(files) or 1))
        copy_env = get_psql_env(db_url, get_bulk_copy_env(log))

        def copy_one(file_path: str) -> str:
            r"""``\copy`` one chunk file and return its path."""
            log.debug(f"Copy data from {file_path} into database...")
            cmd = [
                "psql",
                psql_url,
                "-c",
                f"\\copy {AppBase.metadata.schema}.ukhpucl {selected_columns} FROM '{file_path}' WITH (FORMAT CSV, HEADER TRUE, {force_null_str}, ON_ERROR 'ignore')",
            ]
            subprocess.run(cmd, check=True, env=copy_env)
            log.debug(f"Successfully copied data from {file_path} into database!")
            return file_path

        log.info(
            f"Copying {len(files)} chunk(s) into the database, {jobs} at a time..."
        )
        if jobs == 1:
            for file_path in files:
                copy_one(file_path)
        else:
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                list(pool.map(copy_one, files))

        if self.resolve_mode == "smart":
            for fp in files:
                cmd = f"rm {fp}"
                subprocess.run(cmd, shell=True, check=True)

    def _nuke_copy_db(self, log=None):
        """Copy the whole CSV in a single pass (not implemented)."""
        raise NotImplementedError("WIP")

    def _chunked_copy(self, log=None):
        """Run a chunked copy; see `_copy_chunked_data_to_db`."""
        cfg = get_cfg()
        if log is None:
            log = setup_logger(cfg)
        log.debug(f"Successfully initialized {log.name}")
        db_url = get_db_url(log)
        self._set_postgres_variables(db_url, cfg, log)
        directory_path = inputs.resolve("ukhpucl_csv_fp", cfg).parent.joinpath("data_part_")
        if self.input_csv_fp is not None:
            directory_path = Path(self.input_csv_fp).parent.joinpath("data_part_")
        if not self.skip_chunking:
            log.info("Starting preparations for chunked db copy...")
            directory_path = self._prepare_files(cfg, log)
            log.info("Successfully completed preparation for chunked db copy!")
        log.info("Started copying chunked files to db...")
        self._copy_chunked_data_to_db(directory_path, db_url, log)
        log.info("Successfully copied chunked files to db!")

    def _nuke_copy(self, log=None):
        """Run a single-pass copy; see `_nuke_copy_db`."""
        self._nuke_copy_db(log)

    def _copy_with_polars(self, log=None):
        """Run a polars copy; see `_copy_data_to_db_with_polars`."""
        self._copy_data_to_db_with_polars(log)

    def _copy_data_to_db_with_polars(self, log=None):
        """
        Stream the CSV into ``staging.ukhpucl`` in polars batches via COPY FROM STDIN.

        The schema is created if missing but never dropped, because this load
        runs between alembic revisions 0001 and 0002. ``max_csv_rows`` only
        sets how many rows polars samples to infer types; ``max_load_rows``
        truncates the load. The sequential path copies through one raw DBAPI
        connection.

        Returns
        -------
        int
            Rows copied.
        """
        cfg = get_cfg()
        if log is None:
            log = setup_logger(cfg)
        log.debug(f"Successfully initialized {log.name}")
        engine = get_sql_dev_engine(log)

        create_db(engine, log)

        t0 = time.perf_counter()
        staging_csv_fp = self.input_csv_fp
        if self.input_csv_fp is None:
            staging_csv_fp = inputs.resolve("ukhpucl_csv_fp", cfg)

        if not Path(staging_csv_fp).exists():
            raise FileNotFoundError(
                f"Source csv '{staging_csv_fp}' does not exist. Place it at the "
                "ukhpucl_csv_fp path in src/hpldn/backend/migrations/inputs.yaml "
                "(under HPLDN_DATA_DIR), or override data.ukhpucl_csv_fp in "
                "configuration/staging.yaml."
            )

        log.debug("Scanning '%s' (lazy) …", staging_csv_fp)

        infer_schema_length = cfg.get("max_csv_rows", 10_000)
        row_limit = cfg.get("max_load_rows")
        if row_limit is not None:
            log.warning(
                "max_load_rows=%s is set: only the first %s rows of '%s' will be "
                "loaded - unset it in staging.yaml to load the full dataset.",
                row_limit,
                row_limit,
                staging_csv_fp,
            )

        lazy: pl.LazyFrame = pl.scan_csv(
            staging_csv_fp,
            infer_schema_length=infer_schema_length,
            ignore_errors=True,
            try_parse_dates=True,
            truncate_ragged_lines=False,
            n_rows=row_limit,
        )

        table_name = f"{AppBase.metadata.schema}.{UKHPUCL.__tablename__}"
        chunk_size = cfg["chunk_size"]
        total_rows = 0

        with engine.begin() as conn:
            log.debug(f"Truncating {table_name}...")
            conn.execute(text(f"TRUNCATE TABLE {table_name} RESTART IDENTITY"))

        jobs = 1 if self.single_commit else get_copy_jobs(log=log)
        log.info(
            "Copying into %s with %d concurrent connection(s) — %s",
            table_name,
            jobs,
            describe(detect_host_resources(log)),
        )

        if jobs > 1:
            total_rows = self._parallel_polars_copy(
                engine, lazy, table_name, chunk_size, jobs, t0, log
            )
        else:
            raw = engine.raw_connection()
            try:
                for batch_num, chunk in enumerate(
                    self._iter_chunks(lazy, chunk_size), start=1
                ):
                    n = self._polars_copy_into(raw, table_name, chunk)
                    total_rows += n

                    if batch_num % 10 == 0:
                        elapsed = time.perf_counter() - t0
                        log.debug(
                            "  batch %4d | rows so far: %10d | %.0f rows/s",
                            batch_num,
                            total_rows,
                            total_rows / elapsed if elapsed else 0,
                        )
                        if not self.single_commit:
                            raw.commit()
                if self.single_commit:
                    log.warning("Commiting everything to db in a single transaction!")
                raw.commit()
                log.debug("Success: transaction commited to db")
            except Exception:
                raw.rollback()
                raise
            finally:
                raw.close()

        elapsed = time.perf_counter() - t0
        log.info(
            "Done. Inserted %d rows into '%s' in %.1f s (%.0f rows/s).",
            total_rows,
            table_name,
            elapsed,
            total_rows / elapsed if elapsed else 0,
        )

        if total_rows == 0:
            raise RuntimeError(
                f"No rows were copied from '{staging_csv_fp}' into {table_name} - "
                "check that the csv is not empty and that its header matches the "
                "ukhpucl columns."
            )

    def _parallel_polars_copy(
        self,
        engine,
        lazy: pl.LazyFrame,
        table_name: str,
        chunk_size: int,
        jobs: int,
        t0: float,
        log: logging.Logger,
    ) -> int:
        """
        COPY polars batches into PostgreSQL over `jobs` connections at once.

        Batches are submitted `jobs` at a time, capping memory at
        ``jobs * chunk_size`` rows in flight. Each worker thread keeps its own
        raw connection and commits its own batch.

        Parameters
        ----------
        engine : sqlalchemy.Engine
            Engine to open raw connections from.
        lazy : polars.LazyFrame
            Scan of the source CSV.
        table_name : str
            Fully qualified target table.
        chunk_size : int
            Rows per batch, a buffering hint.
        jobs : int
            Concurrent connections.
        t0 : float
            ``time.perf_counter()`` at the start of the load, for rate logs.
        log : logging.Logger
            Logger for progress messages.

        Returns
        -------
        int
            Rows copied.
        """
        thread_state = threading.local()
        connections = []
        connections_lock = threading.Lock()

        def copy_batch(frame: pl.DataFrame) -> int:
            """COPY one batch on this thread's connection and return its row count."""
            conn = getattr(thread_state, "conn", None)
            if conn is None:
                conn = engine.raw_connection()
                thread_state.conn = conn
                with connections_lock:
                    connections.append(conn)
            try:
                n = self._polars_copy_into(conn, table_name, frame)
                conn.commit()
                return n
            except Exception:
                conn.rollback()
                raise

        total_rows = 0
        batch_num = 0
        group = []
        try:
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                for chunk in self._iter_chunks(lazy, chunk_size):
                    group.append(chunk)
                    if len(group) < jobs:
                        continue
                    total_rows += sum(pool.map(copy_batch, group))
                    batch_num += len(group)
                    group.clear()
                    if batch_num % (10 * jobs) == 0:
                        elapsed = time.perf_counter() - t0
                        log.debug(
                            "  batch %4d | rows so far: %10d | %.0f rows/s",
                            batch_num,
                            total_rows,
                            total_rows / elapsed if elapsed else 0,
                        )
                if group:
                    total_rows += sum(pool.map(copy_batch, group))
        finally:
            for conn in connections:
                conn.close()
        log.debug("Success: all batches commited to db")
        return total_rows

    def _polars_copy_into(self, raw_conn, table_name: str, frame: pl.DataFrame) -> int:
        """
        Stream one polars DataFrame into PostgreSQL via COPY FROM STDIN.

        String columns are truncated to the model's declared lengths.

        Parameters
        ----------
        raw_conn : DBAPI connection
            Raw PostgreSQL connection.
        table_name : str
            Target table.
        frame : polars.DataFrame
            Rows to copy.

        Returns
        -------
        int
            Rows copied.
        """
        if frame.is_empty():
            return 0

        for col_name, col_def in UKHPUCL.__table__.columns.items():
            if (
                col_name in frame.columns
                and not isinstance(col_def.type, Integer)
                and not isinstance(col_def.type, Float)
                and isinstance(frame.get_column(col_name).dtype, pl.String)
            ):
                max_len = col_def.type.length
                if max_len is not None:
                    frame = frame.with_columns(
                        pl.col(col_name).str.slice(offset=0, length=max_len)
                    )
        buf = io.BytesIO()
        frame.write_csv(buf)
        buf.seek(0)

        cols = ",".join([f'"{col}"' for col in frame.columns])
        with raw_conn.cursor() as cur:
            cur.copy_expert(
                f"COPY {table_name} ({cols}) FROM STDIN WITH (FORMAT CSV, HEADER TRUE)",
                buf,
            )
        return len(frame)

    def _iter_chunks(
        self, lazy: pl.LazyFrame, chunk_size: int
    ) -> Iterator[pl.DataFrame]:
        """
        Yield batches from a LazyFrame, reading the CSV once.

        Uses the streaming ``collect_batches`` when available, falling back to
        offset slicing. `chunk_size` is a hint, so batch sizes vary.

        Parameters
        ----------
        lazy : polars.LazyFrame
            Scan to iterate over.
        chunk_size : int
            Target rows per batch.

        Yields
        ------
        polars.DataFrame
            Non-empty batches.
        """
        collect_batches = getattr(lazy, "collect_batches", None)
        if collect_batches is not None:
            for chunk in collect_batches(chunk_size=chunk_size):
                if not chunk.is_empty():
                    yield chunk
            return

        offset = 0
        while True:
            chunk: pl.DataFrame = lazy.slice(offset, chunk_size).collect()
            if chunk.is_empty():
                break
            yield chunk
            if len(chunk) < chunk_size:
                break
            offset += chunk_size
