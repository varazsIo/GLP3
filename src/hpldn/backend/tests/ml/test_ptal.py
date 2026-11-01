"""
Tests for the PTAL features in the database, `hpp_rf` and `hpp_cb`.

Database tests read the migrated hpdbldn through `hpdbldn_ro` and check the cell polygons,
``production.ptal_features`` (both the containment join and the bounded
nearest-cell fallback) and the view columns. Synthetic-data tests check both
models median-fill PTAL columns with a shared missing flag, and that `hpp_cb`
imports the column list from `hpp_rf`::

    pytest src/hpldn/backend/tests/ml/test_ptal.py -s
"""


import numpy as np
import pytest
from sqlalchemy import text

from hpldn.backend.scripts import hpp_cb, hpp_rf
from hpldn.backend.tests.ml.test_hp_rf import (
    _make_dataset,
    _make_mortgage_rates,
    _with_mortgage_features,
)

FALLBACK_MAX_DISTANCE_M = 150
DISTANCE_TOLERANCE_M = 5.0


def _scalar(engine, sql: str):
    with engine.connect() as connection:
        return connection.execute(text(sql)).scalar()


def _one(engine, sql: str):
    with engine.connect() as connection:
        return connection.execute(text(sql)).one()




def test_ptal_cell_polygons_covers_every_grid_cell(hpdbldn_ro):
    """Check there is exactly one polygon per PTAL cell, in both directions."""
    total, matched = _one(
        hpdbldn_ro,
        "SELECT (SELECT COUNT(*) FROM staging.ptal_2023), "
        "(SELECT COUNT(*) FROM staging.ptal_2023 p "
        " JOIN staging.ptal_cell_polygons c ON c.grid_id = p.grid_id)",
    )
    assert total == 159451
    assert matched == total


def test_ptal_cell_polygons_are_multipolygon_srid_4326(hpdbldn_ro):
    """Check every polygon is a valid MultiPolygon in SRID 4326."""
    with hpdbldn_ro.connect() as connection:
        total, non_null, valid, srid_ok, type_ok = connection.execute(
            text(
                "SELECT COUNT(*), COUNT(geom), "
                "COUNT(*) FILTER (WHERE ST_IsValid(geom)), "
                "COUNT(*) FILTER (WHERE ST_SRID(geom) = 4326), "
                "COUNT(*) FILTER (WHERE GeometryType(geom) = 'MULTIPOLYGON') "
                "FROM staging.ptal_cell_polygons"
            )
        ).one()
    assert total == 159451
    assert non_null == total
    assert valid == total
    assert srid_ok == total
    assert type_ok == total


def test_ptal_cell_polygons_geom_gist_index(hpdbldn_ro):
    exists = _scalar(
        hpdbldn_ro,
        "SELECT 1 FROM pg_indexes WHERE schemaname = 'staging' "
        "AND tablename = 'ptal_cell_polygons' "
        "AND indexdef ILIKE '%USING gist%'",
    )
    assert exists == 1




def test_ptal_features_one_row_per_ppd_id(hpdbldn_ro):
    """Check ``ptal_features`` has a row for every PPD transaction, even where values are NULL."""
    ppd_count, feature_count, distinct_ppd_id = _one(
        hpdbldn_ro,
        "SELECT (SELECT COUNT(*) FROM production.ppd_table), "
        "(SELECT COUNT(*) FROM production.ptal_features), "
        "(SELECT COUNT(DISTINCT ppd_id) FROM production.ptal_features)",
    )
    assert feature_count == ppd_count
    assert distinct_ppd_id == feature_count


def test_ptal_features_ppd_id_unique_index(hpdbldn_ro):
    exists = _scalar(
        hpdbldn_ro,
        "SELECT 1 FROM pg_indexes WHERE schemaname = 'production' "
        "AND tablename = 'ptal_features' AND indexdef ILIKE '%UNIQUE%'",
    )
    assert exists == 1


def test_ptal_features_values_within_published_ranges(hpdbldn_ro):
    """Check non-null band ranks are 0-8 and accessibility indices non-negative."""
    with hpdbldn_ro.connect() as connection:
        out_of_range_rank, negative_ai = connection.execute(
            text(
                "SELECT COUNT(*) FILTER (WHERE ptal_band_rank NOT BETWEEN 0 AND 8), "
                "COUNT(*) FILTER (WHERE ptal_ai < 0) "
                "FROM production.ptal_features"
            )
        ).one()
    assert out_of_range_rank == 0
    assert negative_ai == 0


def test_ptal_features_almost_always_resolves_a_cell(hpdbldn_ro):
    """Check nearly every sale resolves a cell (665,746 of 665,753 on this instance)."""
    with hpdbldn_ro.connect() as connection:
        total, resolved = connection.execute(
            text(
                "SELECT COUNT(*), COUNT(ptal_band_rank) FROM production.ptal_features"
            )
        ).one()
    assert total > 0
    assert resolved / total > 0.999


def test_ptal_features_primary_join_agrees_with_direct_containment_query(hpdbldn_ro):
    """Check stored values match an independent containment query for a sample of sales."""
    mismatches = _scalar(
        hpdbldn_ro,
        """
        WITH sample AS (
            SELECT pf.ppd_id, pf.ptal_band_rank, pf.ptal_ai, ac.longitude, ac.latitude
            FROM production.ptal_features pf
            JOIN production.ppd_table p ON p.id = pf.ppd_id
            JOIN production.address_coordinates ac ON ac.address_id = p.address_id
            WHERE pf.ptal_band_rank IS NOT NULL
            ORDER BY pf.ppd_id
            LIMIT 3000
        )
        SELECT COUNT(*)
        FROM sample s
        JOIN staging.ptal_cell_polygons poly
            ON ST_Contains(poly.geom, ST_SetSRID(ST_MakePoint(s.longitude, s.latitude), 4326))
        JOIN staging.ptal_london pl ON pl.grid_id = poly.grid_id
        WHERE s.ptal_band_rank IS DISTINCT FROM pl.ptal_band_rank
           OR s.ptal_ai IS DISTINCT FROM pl.ai
        """,
    )
    assert mismatches == 0


def test_ptal_features_fallback_only_fires_within_bound_and_picks_the_true_nearest_cell(
    hpdbldn_ro,
):
    """
    Check the nearest-cell fallback for properties no polygon contains.

    A resolved row must match the nearest centroid within
    ``FALLBACK_MAX_DISTANCE_M``; a NULL row's nearest centroid must be further.
    All such rows are checked (9 on this instance).
    """
    with hpdbldn_ro.connect() as connection:
        rows = connection.execute(
            text(
                """
                WITH pts AS (
                    SELECT p.id AS ppd_id,
                           ST_SetSRID(ST_MakePoint(ac.longitude, ac.latitude), 4326) AS geom
                    FROM production.ppd_table p
                    JOIN production.address_coordinates ac ON ac.address_id = p.address_id
                    WHERE ac.latitude IS NOT NULL AND ac.longitude IS NOT NULL
                ),
                not_contained AS (
                    SELECT pt.ppd_id, pt.geom
                    FROM pts pt
                    WHERE NOT EXISTS (
                        SELECT 1 FROM staging.ptal_cell_polygons poly
                        WHERE ST_Contains(poly.geom, pt.geom)
                    )
                )
                SELECT
                    nc.ppd_id,
                    pf.ptal_band_rank AS assigned_rank,
                    pf.ptal_ai AS assigned_ai,
                    nearest.ptal_band_rank AS nearest_rank,
                    nearest.ai AS nearest_ai,
                    nearest.distance_m
                FROM not_contained nc
                JOIN production.ptal_features pf ON pf.ppd_id = nc.ppd_id
                CROSS JOIN LATERAL (
                    SELECT pl.ptal_band_rank, pl.ai,
                           ST_Distance(
                               ST_Transform(pl.geom, 27700), ST_Transform(nc.geom, 27700)
                           ) AS distance_m
                    FROM staging.ptal_london pl
                    ORDER BY ST_Transform(pl.geom, 27700) <-> ST_Transform(nc.geom, 27700)
                    LIMIT 1
                ) nearest
                """
            )
        ).all()

    assert len(rows) > 0, "expected at least one property with no containing polygon on this instance"
    for row in rows:
        if row.assigned_rank is not None:
            assert row.distance_m <= FALLBACK_MAX_DISTANCE_M + DISTANCE_TOLERANCE_M, (
                f"ppd_id {row.ppd_id} resolved a cell {row.distance_m:.1f}m away, "
                f"beyond the {FALLBACK_MAX_DISTANCE_M}m fallback bound"
            )
            assert row.assigned_rank == row.nearest_rank
            assert row.assigned_ai == pytest.approx(row.nearest_ai)
        else:
            assert row.distance_m > FALLBACK_MAX_DISTANCE_M - DISTANCE_TOLERANCE_M, (
                f"ppd_id {row.ppd_id} was left NULL but the nearest cell is only "
                f"{row.distance_m:.1f}m away, inside the fallback bound"
            )




def test_london_houses_exposes_ptal_columns(hpdbldn_ro):
    """Check the view exposes the PTAL columns, via ``pg_attribute``."""
    with hpdbldn_ro.connect() as connection:
        rank_exists, ai_exists = connection.execute(
            text(
                "SELECT "
                "COUNT(*) FILTER (WHERE attname = 'ptal_band_rank'), "
                "COUNT(*) FILTER (WHERE attname = 'ptal_ai') "
                "FROM pg_attribute "
                "WHERE attrelid = 'production.london_houses'::regclass "
                "AND attnum > 0 AND NOT attisdropped"
            )
        ).one()
    assert rank_exists == 1
    assert ai_exists == 1


def test_london_houses_ptal_coverage_matches_ptal_features(hpdbldn_ro):
    """Check the view keeps ``ptal_features``' near-total coverage."""
    with hpdbldn_ro.connect() as connection:
        total, populated = connection.execute(
            text("SELECT COUNT(*), COUNT(ptal_band_rank) FROM production.london_houses")
        ).one()
    assert total > 0
    assert populated / total > 0.999




def test_ptal_dependent_columns_defined():
    assert hpp_rf.PTAL_DEPENDENT_COLUMNS == ["ptal_band_rank", "ptal_ai"]


def test_ptal_columns_included_in_feature_columns():
    for col in hpp_rf.PTAL_DEPENDENT_COLUMNS:
        assert col in hpp_rf.FEATURE_COLUMNS
    for col in hpp_rf.PTAL_DEPENDENT_COLUMNS:
        assert col not in hpp_rf.CATEGORICAL_COLUMNS
        assert col not in hpp_rf.NULLABLE_CATEGORICAL_COLUMNS


def test_engineer_features_leaves_ptal_columns_untouched():
    """Check `engineer_features` leaves the numeric PTAL columns NaN for later median filling."""
    raw = _make_dataset(n=300)
    missing = raw["ptal_band_rank"].isna()
    assert missing.any() and (~missing).any(), "fixture should include a missing ptal slice"

    df = hpp_rf.engineer_features(raw)
    assert df.loc[missing, "ptal_band_rank"].isna().all()
    assert df.loc[missing, "ptal_ai"].isna().all()
    assert df.loc[~missing, "ptal_band_rank"].equals(raw.loc[~missing, "ptal_band_rank"])


def _split_and_stat(df, **split_kwargs):
    """Run feature engineering, the split and the price statistics `impute_and_encode` expects."""
    df = _with_mortgage_features(hpp_rf.engineer_features(df))
    train_df, val_df, test_df = hpp_rf.time_series_split(df, **split_kwargs)
    train_df, val_df, test_df, _ = hpp_rf.add_postcode_price_stats(train_df, val_df, test_df)
    return hpp_rf.add_postcode_sector_price_stats(train_df, val_df, test_df)


def test_impute_and_encode_fills_missing_ptal_with_train_median():
    """
    Check PTAL columns are filled with the train median and flagged.

    Every column is located by name in `hpp_rf.feature_layout`'s matrix
    order, never by an offset: the Ofsted categoricals sit before the
    numeric block, so a hand-built prefix drifts whenever a group is added.
    """
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=300), train_frac=0.7, val_frac=0.15)
    X_train, _, _, _, medians, _ = hpp_rf.impute_and_encode(train_df, val_df, test_df)
    assert medians["ptal_band_rank"] == pytest.approx(train_df["ptal_band_rank"].median())
    assert medians["ptal_ai"] == pytest.approx(train_df["ptal_ai"].median())

    matrix_columns = hpp_rf.feature_layout()["matrix_columns"]
    assert X_train.shape[1] == len(matrix_columns)
    rank_idx = matrix_columns.index("ptal_band_rank")
    ai_idx = matrix_columns.index("ptal_ai")
    ptal_missing_idx = matrix_columns.index("ptal_missing")

    missing_rows = train_df["ptal_band_rank"].isna().to_numpy()
    assert missing_rows.any(), "test fixture should include missing ptal_band_rank rows"
    assert np.allclose(X_train[missing_rows, rank_idx], medians["ptal_band_rank"])
    assert np.allclose(X_train[missing_rows, ai_idx], medians["ptal_ai"])
    assert np.array_equal(X_train[:, ptal_missing_idx], missing_rows.astype(np.float64))
    assert np.allclose(
        X_train[~missing_rows, rank_idx], train_df.loc[~missing_rows, "ptal_band_rank"].to_numpy()
    )


def test_run_training_feature_importance_reflects_ptal_signal():
    """Check a forest assigns the synthetic PTAL signal non-negligible importance."""
    df = _make_dataset(n=600, seed=4)
    args = hpp_rf.build_arg_parser().parse_args([])
    args.n_estimators, args.max_depth, args.n_jobs, args.random_state = 50, 10, 1, 0
    df_engineered = hpp_rf.engineer_features(df).sort_values(hpp_rf.DATE_COLUMN).reset_index(drop=True)
    train_df, val_df, test_df = hpp_rf.time_series_split(df_engineered, args.train_frac, args.val_frac)
    model, _, _, _, _, _ = hpp_rf._fit_and_evaluate(train_df, val_df, test_df, args)

    importances = dict(zip(hpp_rf.FEATURE_COLUMNS, model.feature_importances_))
    ptal_importance = importances["ptal_band_rank"] + importances["ptal_ai"]
    assert ptal_importance > 0.001




def test_hpp_cb_imports_ptal_dependent_columns_from_hpp_rf_not_its_own_copy():
    """Check `hpp_cb` uses `hpp_rf`'s PTAL column list itself, by identity."""
    assert hpp_cb.PTAL_DEPENDENT_COLUMNS is hpp_rf.PTAL_DEPENDENT_COLUMNS


def test_hpp_cb_missing_flag_groups_includes_ptal():
    assert hpp_cb._MISSING_FLAG_GROUPS["ptal_missing"] is hpp_rf.PTAL_DEPENDENT_COLUMNS


def test_prepare_features_fills_missing_ptal_with_train_median():
    """
    Check CatBoost's `prepare_features` median-fills PTAL columns with a flag.

    ``ptal_band_rank`` is dropped by default as a binning of ``ptal_ai``, so
    only ``ptal_ai`` and its flag remain.
    """
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=300), train_frac=0.7, val_frac=0.15)
    X_train, _, _, _, medians, _ = hpp_cb.prepare_features(train_df, val_df, test_df)
    assert "ptal_band_rank" not in X_train.columns
    assert "ptal_band_rank" not in medians
    assert medians["ptal_ai"] == pytest.approx(train_df["ptal_ai"].median())

    missing_rows = train_df["ptal_ai"].isna().to_numpy()
    assert missing_rows.any(), "test fixture should include missing ptal_ai rows"
    assert np.allclose(X_train.loc[missing_rows, "ptal_ai"].to_numpy(), medians["ptal_ai"])
    assert np.array_equal(X_train["ptal_missing"].to_numpy(), missing_rows.astype(np.float64))


def test_prepare_features_ptal_columns_pass_through_unmodified_when_present():
    train_df, val_df, test_df, _ = _split_and_stat(_make_dataset(n=300), train_frac=0.7, val_frac=0.15)
    X_train, _, _, _, _, _ = hpp_cb.prepare_features(train_df, val_df, test_df)
    missing_rows = train_df["ptal_ai"].isna().to_numpy()
    assert np.allclose(
        X_train.loc[~missing_rows, "ptal_ai"].to_numpy(),
        train_df.loc[~missing_rows, "ptal_ai"].to_numpy(),
    )


def test_run_training_end_to_end_on_synthetic_data_with_ptal():
    """Check CatBoost trains end to end on the PTAL-carrying synthetic data."""
    df = _make_dataset(n=600, seed=1)
    args = hpp_cb.build_arg_parser().parse_args([])
    args.device, args.iterations, args.early_stopping_rounds = "cpu", 50, 10
    args.thread_count, args.random_state = 1, 0
    results = hpp_cb.run_training(df, args, _make_mortgage_rates())
    assert results["n_train"] + results["n_val"] + results["n_test"] == len(df)
    assert results["val"]["mae"] > 0
