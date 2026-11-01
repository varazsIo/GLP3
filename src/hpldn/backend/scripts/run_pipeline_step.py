"""
Run one named, one-off HPLDN pipeline step.

Usage::

    python -m hpldn.backend.scripts.run_pipeline_step --list
    python -m hpldn.backend.scripts.run_pipeline_step <step_name>
"""

import argparse
import os
import sys
from pathlib import Path

from hpldn.backend.common.db import get_sql_dev_engine
from hpldn.backend.common.config import get_cfg
from hpldn.backend.common.utils import setup_logger
from hpldn.backend.scripts import pipeline_steps as steps

STEPS = {
    "drop_all_tables": steps.drop_all_tables,
    "create_ukhpucl_indexes": steps.create_ukhpucl_indexes,
    "create_london_houses_table": steps.create_london_houses_table,
    "populate_ukhcs_table": steps.populate_ukhcs_table,
    "create_address_table": steps.create_address_table,
    "add_poststreet_to_address_table": steps.add_poststreet_to_address_table,
    "create_london_houses_condensed_table": steps.create_london_houses_condensed_table,
    "load_ppd_table": steps.load_ppd_table,
    "load_epc_table_condensed": steps.load_epc_table_condensed,
    "create_epc_fused_staging_table": steps.create_epc_fused_staging_table,
    "insert_epc_data_into_db": steps.insert_epc_data_into_db,
    "insert_inspire_polygon_data": steps.insert_inspire_polygon_data,
    "promote_staging_to_production": steps.promote_staging_to_production,
    "create_comps_features_table": steps.create_comps_features_table,
    "create_london_houses_materialized_view": steps.create_london_houses_materialized_view,
    "create_api_readonly_role": steps.create_api_readonly_role,
}


def main() -> None:
    """Parse the step name, then run it with the configured engine, cfg and logger."""
    parser = argparse.ArgumentParser(
        description=(
            "Run a single, explicitly-named one-off HPLDN staging pipeline "
            "step (see hpldn.backend.scripts.pipeline_steps). "
            "These are slow, destructive and/or "
            "non-idempotent, only run a step when you are sure it hasn't "
            "already been run against this database."
        )
    )
    parser.add_argument(
        "step",
        nargs="?",
        choices=sorted(STEPS.keys()),
        help="Name of the pipeline step to run",
    )
    parser.add_argument(
        "--list", action="store_true", help="List available step names and exit"
    )
    args = parser.parse_args()

    if args.list or not args.step:
        print("Available pipeline steps:")
        for name in sorted(STEPS.keys()):
            print(f"  {name}")
        sys.exit(0 if args.list else 1)

    cfg = get_cfg()
    log = setup_logger(cfg)
    engine = get_sql_dev_engine()

    step_fn = STEPS[args.step]
    log.warning(
        f"Running pipeline step '{args.step}' this may be a slow, "
        "destructive, non-idempotent operation."
    )
    step_fn(engine, cfg, log)
    log.info(f"Pipeline step '{args.step}' completed.")


if __name__ == "__main__":
    main()
