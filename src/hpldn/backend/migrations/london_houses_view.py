"""
The definitions of ``production.london_houses``.

Two versions live here, because a from-empty replay builds both in turn:

``CREATE_VIEW_SQL_0020``
    Built by migration 0016 and rebuilt by 0020 (93 columns, 18 joins,
    predicate ``a.postcode IS NOT NULL AND p.price >= 10000``), with one
    change from 0020: ``is_maisonette`` falls back to the
    as-of certificate's ``property_type`` when ukhpucl has no row for the
    sale, which is every sale after October 2024.

``CREATE_VIEW_SQL_0025``
    Built by migration 0025 and by 0028/0029 (`link_carry.rebuild_production`).
    The certificate's time-invariant fields (floor area, rooms, build form,
    age band, floor level, property type) fall back to the nearest
    certificate inspected *after* the sale when none before it carries a
    floor area; the rating, tenure and the other fields stay as of the sale.
    ``epc_age_at_sale_days`` is signed (negative for a later certificate),
    and two columns are appended: ``street_key``
    (`STREET_KEY_FUNCTION_SQL`) and ``epc_after_sale``.

``CREATE_VIEW_SQL``
    The current definition, built by migration 0034 and by ``pipeline_steps``:
    ``CREATE_VIEW_SQL_0025`` plus the twelve ``ofsted_*`` school columns of
    ``production.ofsted_features`` (`OFSTED_VIEW_COLUMNS`), appended. A
    revision before 0034 must build its own frozen definition: the table it
    joins does not exist yet on a from-empty replay.

``epc_matched`` marks sales with a usable pre-sale certificate in both.
"""

# Parity targets asserted by migration 0016 on a from-empty build. 0017-0020
# add sales, so 0020 asserts these as floors, not exact counts.
LONDON_HOUSES_ROWS_EXPECTED = 1_923_529

EPC_MATCHED_ROWS_EXPECTED = 891_187

DROP_VIEW_SQL = "DROP MATERIALIZED VIEW IF EXISTS production.london_houses"

# Supporting index for the view's as-of EPC lateral.
EPC_ASOF_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_prod_epc_address_inspection "
    "ON production.epc_table (address_id, inspection_date DESC)"
)

# Composite index for the view's per-row ukhpucl lookup on (UPRN, price, date);
# without it the build plans at ~180M cost and runs for hours.
UKHPUCL_UPRN_PRICE_INDEX_SQL = (
    'CREATE INDEX IF NOT EXISTS idx_uk_uprn_price '
    'ON staging.ukhpucl ("UPRN", price)'
)

# The same-street group key: non-breaking spaces read as spaces, upper-cased,
# a leading "FLAT x," and a leading house number or range ("13", "13A,",
# "100-102") removed, whitespace collapsed, NULL when blank. The patterns are
# hpp_rf.STREET_KEY_PREFIX_PATTERN and STREET_KEY_WHITESPACE_PATTERN verbatim
# (explicit ASCII classes, so both engines read them alike), which
# hpp_rf.normalise_street_key applies in Python; migration 0025 checks the two
# agree on every street in production.address_table.
STREET_KEY_FUNCTION_SQL = r"""
CREATE OR REPLACE FUNCTION production.hpldn_street_key(street text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT NULLIF(btrim(regexp_replace(
        regexp_replace(
            upper(replace(street, chr(160), ' ')),
            '^[ \t\n\r\f\v]*(FLAT[ \t\n\r\f\v]+[A-Z0-9_]+[ \t\n\r\f\v]*,?[ \t\n\r\f\v]*)?([0-9]+[A-Z]?(-[0-9]+[A-Z]?)?(?![A-Z0-9_])[ \t\n\r\f\v]*,?[ \t\n\r\f\v]*)?',
            ''
        ),
        '[ \t\n\r\f\v]+', ' ', 'g'
    ), ' '), '')
$$
"""

CREATE_VIEW_SQL_0020 = """
CREATE MATERIALIZED VIEW production.london_houses AS
SELECT p.id AS ppd_id,
    p.address_id,
    ul.uprn,
    p.price,
    p.dateoftransfer,
    p.propertytype,
    p.duration,
    p.oldnew,
    split_part(a.postcode::text, ' '::text, 1) AS postcode_district,
    (split_part(a.postcode::text, ' '::text, 1) || '_'::text) || "left"(split_part(a.postcode::text, ' '::text, 2), 1) AS postcode_sector,
    a.poststreet,
    e.construction_age_band,
    e.number_habitable_rooms,
    e.current_energy_rating,
    e.total_floor_area,
    bf.build_form,
    e.mains_gas_flag,
    e.tenure,
    e.multi_glaze_proportion,
    e.extension_count,
    e.floor_level,
    p.dateoftransfer - e.inspection_date AS epc_age_at_sale_days,
    ac.latitude,
    ac.longitude,
    lp.lot_area_sqm,
    cf.comp_median_price,
    cf.comp_median_price_per_sqm,
    cf.comp_txn_count,
    spf.nearest_station_distance_km,
    spf.nearest_station_zone,
    boe.rate AS boe_rate_at_sale,
    uh.ukhpi_avg_price_at_sale,
    pf.ptal_band_rank,
    pf.ptal_ai,
    rpf.distance_to_river_m AS distance_to_river,
    lmf.lsoa_code,
    lmf.lsoa_name,
    lmf.msoa_code,
    lmf.msoa_name,
    COALESCE(bf.build_form, 'UNKNOWN') || '_' || p.propertytype AS build_form_propertytype,
    COALESCE(e.construction_age_band, 'UNKNOWN') || '_' || p.propertytype AS construction_age_band_propertytype,
    COALESCE(mai.is_maisonette, CASE WHEN e.property_type IS NOT NULL THEN p.propertytype = 'F' AND e.property_type = 'Maisonette' END) AS is_maisonette,
    psf.prev_sale_price,
    psf.prev_sale_date,
    psf.years_since_prev_sale,
    nf.spatial_lag_price,
    nf.median_price_per_sqm_20nn_6m,
    nf.recency_weighted_price,
    nf.proptype_local_median_800m_6m,
    nf.floor_area_percentile_local,
    nf.epc_band_share_a,
    nf.epc_band_share_b,
    nf.epc_band_share_c,
    nf.epc_band_share_d,
    nf.epc_band_share_e,
    nf.epc_band_share_f,
    nf.epc_band_share_g,
    ltf.msoa_price_momentum_1y,
    ltf.msoa_price_momentum_5y,
    iodf.iod_release_year,
    iodf.iod_release_age_years,
    iodf.iod_imd_score,
    iodf.iod_imd_pct_london,
    iodf.iod_imd_decile_national,
    iodf.iod_income_score,
    iodf.iod_income_pct_london,
    iodf.iod_employment_score,
    iodf.iod_education_score,
    iodf.iod_education_pct_london,
    iodf.iod_health_score,
    iodf.iod_crime_score,
    iodf.iod_barriers_score,
    iodf.iod_wider_barriers_score,
    iodf.iod_geo_barriers_score,
    iodf.iod_living_env_score,
    iodf.iod_indoors_score,
    iodf.iod_outdoors_score,
    iodf.iod_outdoors_pct_london,
    iodf.iod_idaci_score,
    iodf.iod_idaopi_score,
    iodf.iod_children_score,
    iodf.iod_adult_skills_score,
    iodf.iod_imd_score_vs_lad,
    iodf.iod_imd_contrast_1km,
    iodf.iod_income_contrast_1km,
    iodf.iod_prev_release_year,
    iodf.iod_imd_change,
    iodf.iod_income_change,
    iodf.iod_education_change,
    iodf.iod_living_env_change,
    iodf.iod_imd_pct_london_change,
    iodf.iod_years_between_releases,
    (e.total_floor_area IS NOT NULL AND e.current_energy_rating IS NOT NULL) AS epc_matched
   FROM production.ppd_table p
     JOIN production.address_table a ON a.id = p.address_id
     LEFT JOIN LATERAL ( SELECT ec.construction_age_band,
            ec.number_habitable_rooms,
            ec.current_energy_rating,
            ec.total_floor_area,
            ec.build_form,
            ec.mains_gas_flag,
            ec.tenure,
            ec.multi_glaze_proportion,
            ec.extension_count,
            ec.floor_level,
            ec.property_type,
            ec.inspection_date
           FROM production.epc_table ec
          WHERE ec.address_id = p.address_id AND ec.inspection_date IS NOT NULL AND ec.inspection_date <= p.dateoftransfer
          ORDER BY ec.inspection_date DESC, ec.lodgement_datetime DESC NULLS LAST, ec.id DESC
         LIMIT 1) e ON true
     LEFT JOIN production.uprn_lookup ul ON ul.address_id = p.address_id
     LEFT JOIN production.build_form_category_table bf ON bf.id = e.build_form
     LEFT JOIN production.address_coordinates ac ON ac.address_id = p.address_id
     LEFT JOIN production.comps_features cf ON cf.ppd_id = p.id
     LEFT JOIN production.station_proximity_features spf ON spf.ppd_id = p.id
     LEFT JOIN production.ptal_features pf ON pf.ppd_id = p.id
     LEFT JOIN production.river_proximity_features rpf ON rpf.ppd_id = p.id
     LEFT JOIN production.lsoa_msoa_features lmf ON lmf.ppd_id = p.id
     LEFT JOIN production.neighbourhood_features nf ON nf.ppd_id = p.id
     LEFT JOIN production.lsoa_price_trend_features ltf ON ltf.ppd_id = p.id
     LEFT JOIN production.iod_features iodf ON iodf.ppd_id = p.id
     LEFT JOIN production.prev_sale_features psf ON psf.ppd_id = p.id
     LEFT JOIN LATERAL ( SELECT (u."PROPERTY_TYPE" = 'Maisonette') AS is_maisonette
           FROM staging.ukhpucl u
          WHERE u."UPRN" = ul.uprn AND u.dateoftransfer::date = p.dateoftransfer AND u.price = p.price
          ORDER BY u."PROPERTY_TYPE"
         LIMIT 1) mai ON true
     LEFT JOIN LATERAL ( SELECT br.rate
           FROM production.boe_rate br
          WHERE br.date_changed <= p.dateoftransfer
          ORDER BY br.date_changed DESC
         LIMIT 1) boe ON true
     LEFT JOIN LATERAL ( SELECT COALESCE(
                CASE p.propertytype
                    WHEN 'D'::bpchar THEN u.avg_price_detached
                    WHEN 'S'::bpchar THEN u.avg_price_semi_detached
                    WHEN 'T'::bpchar THEN u.avg_price_terraced
                    WHEN 'F'::bpchar THEN u.avg_price_flats_maisonettes
                    ELSE NULL::numeric
                END, u.avg_price_all_property_types) AS ukhpi_avg_price_at_sale
           FROM production.ukhpi u
          WHERE u.published_date <= p.dateoftransfer
          ORDER BY u.published_date DESC, u.period DESC
         LIMIT 1) uh ON true
     LEFT JOIN ( SELECT DISTINCT ON (london_parcels.uprn) london_parcels.uprn,
            london_parcels.lot_area_sqm
           FROM production.london_parcels
          ORDER BY london_parcels.uprn, london_parcels.lot_area_sqm DESC) lp ON lp.uprn = ul.uprn
  WHERE a.postcode IS NOT NULL AND p.price >= 10000
"""

# Migration 0025's definition (module docstring): `CREATE_VIEW_SQL_0020` plus
# the later-certificate fallback ``el``, the merged time-invariant fields ``t``,
# ``street_key`` and ``epc_after_sale``.
CREATE_VIEW_SQL_0025 = """
CREATE MATERIALIZED VIEW production.london_houses AS
SELECT p.id AS ppd_id,
    p.address_id,
    ul.uprn,
    p.price,
    p.dateoftransfer,
    p.propertytype,
    p.duration,
    p.oldnew,
    split_part(a.postcode::text, ' '::text, 1) AS postcode_district,
    (split_part(a.postcode::text, ' '::text, 1) || '_'::text) || "left"(split_part(a.postcode::text, ' '::text, 2), 1) AS postcode_sector,
    a.poststreet,
    t.construction_age_band,
    t.number_habitable_rooms,
    e.current_energy_rating,
    t.total_floor_area,
    bf.build_form,
    e.mains_gas_flag,
    e.tenure,
    e.multi_glaze_proportion,
    e.extension_count,
    t.floor_level,
    p.dateoftransfer - t.inspection_date AS epc_age_at_sale_days,
    ac.latitude,
    ac.longitude,
    lp.lot_area_sqm,
    cf.comp_median_price,
    cf.comp_median_price_per_sqm,
    cf.comp_txn_count,
    spf.nearest_station_distance_km,
    spf.nearest_station_zone,
    boe.rate AS boe_rate_at_sale,
    uh.ukhpi_avg_price_at_sale,
    pf.ptal_band_rank,
    pf.ptal_ai,
    rpf.distance_to_river_m AS distance_to_river,
    lmf.lsoa_code,
    lmf.lsoa_name,
    lmf.msoa_code,
    lmf.msoa_name,
    COALESCE(bf.build_form, 'UNKNOWN') || '_' || p.propertytype AS build_form_propertytype,
    COALESCE(t.construction_age_band, 'UNKNOWN') || '_' || p.propertytype AS construction_age_band_propertytype,
    COALESCE(mai.is_maisonette, CASE WHEN t.property_type IS NOT NULL THEN p.propertytype = 'F' AND t.property_type = 'Maisonette' END) AS is_maisonette,
    psf.prev_sale_price,
    psf.prev_sale_date,
    psf.years_since_prev_sale,
    nf.spatial_lag_price,
    nf.median_price_per_sqm_20nn_6m,
    nf.recency_weighted_price,
    nf.proptype_local_median_800m_6m,
    nf.floor_area_percentile_local,
    nf.epc_band_share_a,
    nf.epc_band_share_b,
    nf.epc_band_share_c,
    nf.epc_band_share_d,
    nf.epc_band_share_e,
    nf.epc_band_share_f,
    nf.epc_band_share_g,
    ltf.msoa_price_momentum_1y,
    ltf.msoa_price_momentum_5y,
    iodf.iod_release_year,
    iodf.iod_release_age_years,
    iodf.iod_imd_score,
    iodf.iod_imd_pct_london,
    iodf.iod_imd_decile_national,
    iodf.iod_income_score,
    iodf.iod_income_pct_london,
    iodf.iod_employment_score,
    iodf.iod_education_score,
    iodf.iod_education_pct_london,
    iodf.iod_health_score,
    iodf.iod_crime_score,
    iodf.iod_barriers_score,
    iodf.iod_wider_barriers_score,
    iodf.iod_geo_barriers_score,
    iodf.iod_living_env_score,
    iodf.iod_indoors_score,
    iodf.iod_outdoors_score,
    iodf.iod_outdoors_pct_london,
    iodf.iod_idaci_score,
    iodf.iod_idaopi_score,
    iodf.iod_children_score,
    iodf.iod_adult_skills_score,
    iodf.iod_imd_score_vs_lad,
    iodf.iod_imd_contrast_1km,
    iodf.iod_income_contrast_1km,
    iodf.iod_prev_release_year,
    iodf.iod_imd_change,
    iodf.iod_income_change,
    iodf.iod_education_change,
    iodf.iod_living_env_change,
    iodf.iod_imd_pct_london_change,
    iodf.iod_years_between_releases,
    (e.total_floor_area IS NOT NULL AND e.current_energy_rating IS NOT NULL) AS epc_matched,
    production.hpldn_street_key(a.poststreet::text) AS street_key,
    (el.inspection_date IS NOT NULL) AS epc_after_sale
   FROM production.ppd_table p
     JOIN production.address_table a ON a.id = p.address_id
     LEFT JOIN LATERAL ( SELECT ec.construction_age_band,
            ec.number_habitable_rooms,
            ec.current_energy_rating,
            ec.total_floor_area,
            ec.build_form,
            ec.mains_gas_flag,
            ec.tenure,
            ec.multi_glaze_proportion,
            ec.extension_count,
            ec.floor_level,
            ec.property_type,
            ec.inspection_date
           FROM production.epc_table ec
          WHERE ec.address_id = p.address_id AND ec.inspection_date IS NOT NULL AND ec.inspection_date <= p.dateoftransfer
          ORDER BY ec.inspection_date DESC, ec.lodgement_datetime DESC NULLS LAST, ec.id DESC
         LIMIT 1) e ON true
     LEFT JOIN LATERAL ( SELECT ec.construction_age_band,
            ec.number_habitable_rooms,
            ec.total_floor_area,
            ec.build_form,
            ec.floor_level,
            ec.property_type,
            ec.inspection_date
           FROM production.epc_table ec
          WHERE e.total_floor_area IS NULL AND ec.address_id = p.address_id AND ec.inspection_date > p.dateoftransfer AND ec.total_floor_area IS NOT NULL
          ORDER BY ec.inspection_date, ec.lodgement_datetime DESC NULLS LAST, ec.id DESC
         LIMIT 1) el ON true
     CROSS JOIN LATERAL ( SELECT COALESCE(e.construction_age_band, el.construction_age_band) AS construction_age_band,
            COALESCE(e.number_habitable_rooms, el.number_habitable_rooms) AS number_habitable_rooms,
            COALESCE(e.total_floor_area, el.total_floor_area) AS total_floor_area,
            COALESCE(e.build_form, el.build_form) AS build_form,
            COALESCE(e.floor_level, el.floor_level) AS floor_level,
            COALESCE(e.property_type, el.property_type) AS property_type,
            CASE WHEN el.inspection_date IS NOT NULL THEN el.inspection_date ELSE e.inspection_date END AS inspection_date) t
     LEFT JOIN production.uprn_lookup ul ON ul.address_id = p.address_id
     LEFT JOIN production.build_form_category_table bf ON bf.id = t.build_form
     LEFT JOIN production.address_coordinates ac ON ac.address_id = p.address_id
     LEFT JOIN production.comps_features cf ON cf.ppd_id = p.id
     LEFT JOIN production.station_proximity_features spf ON spf.ppd_id = p.id
     LEFT JOIN production.ptal_features pf ON pf.ppd_id = p.id
     LEFT JOIN production.river_proximity_features rpf ON rpf.ppd_id = p.id
     LEFT JOIN production.lsoa_msoa_features lmf ON lmf.ppd_id = p.id
     LEFT JOIN production.neighbourhood_features nf ON nf.ppd_id = p.id
     LEFT JOIN production.lsoa_price_trend_features ltf ON ltf.ppd_id = p.id
     LEFT JOIN production.iod_features iodf ON iodf.ppd_id = p.id
     LEFT JOIN production.prev_sale_features psf ON psf.ppd_id = p.id
     LEFT JOIN LATERAL ( SELECT (u."PROPERTY_TYPE" = 'Maisonette') AS is_maisonette
           FROM staging.ukhpucl u
          WHERE u."UPRN" = ul.uprn AND u.dateoftransfer::date = p.dateoftransfer AND u.price = p.price
          ORDER BY u."PROPERTY_TYPE"
         LIMIT 1) mai ON true
     LEFT JOIN LATERAL ( SELECT br.rate
           FROM production.boe_rate br
          WHERE br.date_changed <= p.dateoftransfer
          ORDER BY br.date_changed DESC
         LIMIT 1) boe ON true
     LEFT JOIN LATERAL ( SELECT COALESCE(
                CASE p.propertytype
                    WHEN 'D'::bpchar THEN u.avg_price_detached
                    WHEN 'S'::bpchar THEN u.avg_price_semi_detached
                    WHEN 'T'::bpchar THEN u.avg_price_terraced
                    WHEN 'F'::bpchar THEN u.avg_price_flats_maisonettes
                    ELSE NULL::numeric
                END, u.avg_price_all_property_types) AS ukhpi_avg_price_at_sale
           FROM production.ukhpi u
          WHERE u.published_date <= p.dateoftransfer
          ORDER BY u.published_date DESC, u.period DESC
         LIMIT 1) uh ON true
     LEFT JOIN ( SELECT DISTINCT ON (london_parcels.uprn) london_parcels.uprn,
            london_parcels.lot_area_sqm
           FROM production.london_parcels
          ORDER BY london_parcels.uprn, london_parcels.lot_area_sqm DESC) lp ON lp.uprn = ul.uprn
  WHERE a.postcode IS NOT NULL AND p.price >= 10000
"""

# The Ofsted school features migration 0034 appends (`scripts.ofsted_features`).
OFSTED_VIEW_COLUMNS = (
    "ofsted_primary_nearest_m",
    "ofsted_primary_nearest_score",
    "ofsted_primary_nearest_age_years",
    "ofsted_secondary_nearest_m",
    "ofsted_secondary_nearest_score",
    "ofsted_secondary_nearest_age_years",
    "ofsted_primary_good_plus_1km",
    "ofsted_primary_outstanding_1km",
    "ofsted_secondary_good_plus_2km",
    "ofsted_primary_idw_score_1500m",
    "ofsted_secondary_idw_score_3km",
    "ofsted_nearest_primary_basis",
)

_LAST_0025_COLUMN = "    (el.inspection_date IS NOT NULL) AS epc_after_sale\n"
_LAST_0025_JOIN = "     LEFT JOIN production.prev_sale_features psf ON psf.ppd_id = p.id\n"
for _anchor in (_LAST_0025_COLUMN, _LAST_0025_JOIN):
    if CREATE_VIEW_SQL_0025.count(_anchor) != 1:
        raise AssertionError(f"CREATE_VIEW_SQL_0025 must hold {_anchor!r} exactly once")

# The current definition (module docstring): 0025's plus the Ofsted columns.
CREATE_VIEW_SQL = CREATE_VIEW_SQL_0025.replace(
    _LAST_0025_COLUMN,
    _LAST_0025_COLUMN.rstrip("\n") + ",\n"
    + ",\n".join(f"    ofs.{column}" for column in OFSTED_VIEW_COLUMNS) + "\n",
).replace(
    _LAST_0025_JOIN,
    _LAST_0025_JOIN + "     LEFT JOIN production.ofsted_features ofs ON ofs.ppd_id = p.id\n",
)

# Five indexes; the UNIQUE ppd_id one enables REFRESH ... CONCURRENTLY.
CREATE_INDEXES_SQL = """
CREATE UNIQUE INDEX idx_prod_london_houses_ppd_id ON production.london_houses (ppd_id);
CREATE INDEX idx_prod_london_houses_dateoftransfer ON production.london_houses (dateoftransfer);
CREATE INDEX idx_prod_london_houses_postcode_district ON production.london_houses (postcode_district);
CREATE INDEX idx_prod_london_houses_iod_release_year ON production.london_houses (iod_release_year);
CREATE INDEX idx_prod_london_houses_epc_matched ON production.london_houses (epc_matched);
"""

def _select_list_columns(sql: str):
    """
    Return the output column names of a view's top-level SELECT list.

    Reads only between ``SELECT`` and the top-level ``FROM``, taking the
    ``AS`` alias when present and the trailing identifier otherwise. Nested
    SELECTs are indented further and so excluded.
    """
    names, started = [], False
    for line in sql.splitlines():
        stripped = line.strip()
        if not started:
            if stripped.startswith("SELECT "):
                started = True
                stripped = stripped[len("SELECT "):]
            else:
                continue
        if stripped.startswith("FROM "):
            break
        stripped = stripped.rstrip(",")
        if not stripped:
            continue
        if " AS " in stripped:
            names.append(stripped.rsplit(" AS ", 1)[1].strip())
        else:
            names.append(stripped.split(".")[-1].strip())
    return tuple(names)


VIEW_COLUMNS_0020 = _select_list_columns(CREATE_VIEW_SQL_0020)

VIEW_COLUMN_COUNT_0020 = 93

VIEW_COLUMNS_0025 = _select_list_columns(CREATE_VIEW_SQL_0025)

# 0020's 93 plus street_key and epc_after_sale, appended so every earlier
# column keeps its ordinal position.
VIEW_COLUMN_COUNT_0025 = 95

VIEW_COLUMNS = _select_list_columns(CREATE_VIEW_SQL)

# 0025's 95 plus the twelve Ofsted columns, appended.
VIEW_COLUMN_COUNT = 107

for _name, _columns, _count in (
    ("CREATE_VIEW_SQL_0020", VIEW_COLUMNS_0020, VIEW_COLUMN_COUNT_0020),
    ("CREATE_VIEW_SQL_0025", VIEW_COLUMNS_0025, VIEW_COLUMN_COUNT_0025),
    ("CREATE_VIEW_SQL", VIEW_COLUMNS, VIEW_COLUMN_COUNT),
):
    if len(_columns) != _count:
        raise AssertionError(
            f"{_name} yields {len(_columns)} output columns, expected {_count}: {_columns}"
        )
if VIEW_COLUMNS_0025[:VIEW_COLUMN_COUNT_0020] != VIEW_COLUMNS_0020:
    raise AssertionError("CREATE_VIEW_SQL_0025 must keep CREATE_VIEW_SQL_0020's columns, in order, first")
if VIEW_COLUMNS != VIEW_COLUMNS_0025 + OFSTED_VIEW_COLUMNS:
    raise AssertionError("CREATE_VIEW_SQL must be CREATE_VIEW_SQL_0025's columns then OFSTED_VIEW_COLUMNS")


def rebuild(engine, cfg=None, log=None, create_sql: str = CREATE_VIEW_SQL) -> int:
    """
    Drop and recreate ``production.london_houses`` and its five indexes.

    Parameters
    ----------
    engine : sqlalchemy.Engine
        Engine to open a dedicated committing connection on.
    cfg : dict, optional
        ``staging.yaml``, for tuning overrides.
    log : logging.Logger, optional
        Logger for progress messages.
    create_sql : str, optional
        The definition to build: the current `CREATE_VIEW_SQL` by default;
        migrations 0016 and 0020 pass `CREATE_VIEW_SQL_0020`, 0025, 0028 and
        0029 `CREATE_VIEW_SQL_0025`.

    Returns
    -------
    int
        The view's row count.

    Notes
    -----
    Runs outside alembic's per-migration transaction, which would otherwise
    hold locks for the whole expensive build, and with bulk session tuning
    applied.
    """
    from sqlalchemy import text

    from hpldn.backend.common.tuning import apply_bulk_session_tuning

    with engine.begin() as connection:
        apply_bulk_session_tuning(connection, cfg, log)
        connection.execute(text(EPC_ASOF_INDEX_SQL))
        connection.execute(text(UKHPUCL_UPRN_PRICE_INDEX_SQL))
        connection.execute(text(STREET_KEY_FUNCTION_SQL))
        connection.execute(text(DROP_VIEW_SQL))
        connection.execute(text(create_sql))
        connection.execute(text(CREATE_INDEXES_SQL))
        connection.execute(text("ANALYZE production.london_houses"))
        rows = connection.execute(
            text("SELECT count(*) FROM production.london_houses")
        ).scalar_one()
    if log is not None:
        log.info("production.london_houses rebuilt with %d row(s)", rows)
    return rows
