"""
Read-only SQL behind the market routes, one ``text()`` constant per use.

Every statement is bound by an index on ``production.london_houses``
(postcode_district, dateoftransfer) or ``production.address_table``
(postcode). The per-request statements run in milliseconds; the window and
district aggregates take up to a few hundred and are cached per worker.
"""

from sqlalchemy import text

# SQL twin of `districts.property_type_key`.
_TYPE_KEY = """
    CASE h.propertytype
        WHEN 'F' THEN CASE WHEN h.is_maisonette THEN 'maisonette' ELSE 'flat' END
        WHEN 'D' THEN 'detached'
        WHEN 'S' THEN 'semi-detached'
        WHEN 'T' THEN 'terraced'
    END
"""

# SQL twin of `districts.type_group`: PPD's F (flats and maisonettes) or a house.
_TYPE_GROUP = """
    CASE WHEN h.propertytype = 'F' THEN 'F' ELSE 'house' END
"""

# Approximate kilometres from (:origin_lat, :origin_lon); equirectangular,
# within 0.1 % of haversine at London's latitude over a few kilometres.
_DISTANCE_KM = """
    111.32 * sqrt(
        power(h.latitude - :origin_lat, 2)
        + power(cos(radians(:origin_lat)) * (h.longitude - :origin_lon), 2)
    )
"""

# The whole view's extent; `repository.fetch_window` derives the window from it.
DATA_WINDOW_SQL = text(
    """
    SELECT max(dateoftransfer) AS latest_sale,
           count(*)            AS total_sales,
           min(dateoftransfer) AS first_sale
      FROM production.london_houses
    """
)

# One row per area since :date_from; :members and :codes are parallel arrays
# mapping each outward code to its area. Supplies what the calendar-year
# figures do not: coverage, the estimate's median £/m², transport, borough,
# EPC band and centre. See api/README.md.
DISTRICT_STATS_SQL = text(
    r"""
    WITH sales AS (
        SELECT d.code,
               h.price,
               h.total_floor_area,
               h.total_floor_area BETWEEN :min_floor_area AND :max_floor_area AS sized,
               h.ptal_band_rank,
               h.nearest_station_distance_km,
               h.current_energy_rating,
               h.lsoa_name,
               h.latitude,
               h.longitude
          FROM unnest(CAST(:members AS text[]), CAST(:codes AS text[])) AS d(member, code)
          JOIN production.london_houses h ON h.postcode_district = d.member
         WHERE h.dateoftransfer >= :date_from
    )
    SELECT code,
           count(*) AS sales_count,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY price / total_floor_area)
               FILTER (WHERE sized) AS price_per_sqm,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY ptal_band_rank) AS ptal_band_rank,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY nearest_station_distance_km)
               AS station_distance_km,
           mode() WITHIN GROUP (ORDER BY current_energy_rating) AS epc_band,
           mode() WITHIN GROUP (ORDER BY regexp_replace(lsoa_name, '\s+\S+$', ''))
               AS local_authority,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY latitude) AS centre_latitude,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY longitude) AS centre_longitude
      FROM sales
     GROUP BY code
    """
)

# One row per area: the mean price of its sales in calendar years :year,
# :year - 1 and :year - 5, and for :year the mean floor area and mean London
# deprivation percentile. These are the figures the page shows; see
# api/README.md. :members and :codes as in DISTRICT_STATS_SQL.
AREA_YEAR_STATS_SQL = text(
    """
    WITH sales AS (
        SELECT d.code,
               extract(YEAR FROM h.dateoftransfer)::int AS year,
               h.price,
               h.total_floor_area,
               h.iod_imd_pct_london
          FROM unnest(CAST(:members AS text[]), CAST(:codes AS text[])) AS d(member, code)
          JOIN production.london_houses h ON h.postcode_district = d.member
         WHERE h.dateoftransfer >= make_date(:year - 5, 1, 1)
           AND h.dateoftransfer < make_date(:year + 1, 1, 1)
    )
    SELECT code,
           count(*) FILTER (WHERE year = :year)     AS sales_count,
           count(*) FILTER (WHERE year = :year - 1) AS sales_count_previous,
           count(*) FILTER (WHERE year = :year - 5) AS sales_count_5y_ago,
           avg(price) FILTER (WHERE year = :year)     AS average_price,
           avg(price) FILTER (WHERE year = :year - 1) AS average_price_previous,
           avg(price) FILTER (WHERE year = :year - 5) AS average_price_5y_ago,
           avg(total_floor_area) FILTER (WHERE year = :year) AS average_floor_area,
           avg(iod_imd_pct_london) FILTER (WHERE year = :year) AS imd_pct_london
      FROM sales
     GROUP BY code
    """
)

# Every borough's mean monthly private rent, overall and by bedrooms (4 = four
# or more), for its latest month with an overall figure. One row per borough;
# ``area_name`` equals ``london_houses.local_authority``.
LONDON_RENTS_SQL = text(
    """
    WITH latest AS (
        SELECT area_code, max(month) AS month
          FROM production.london_rents
         WHERE area_code LIKE 'E09%'
           AND measure = 'all'
           AND rental_price IS NOT NULL
         GROUP BY area_code
    )
    SELECT r.area_name,
           r.month,
           max(r.rental_price) FILTER (WHERE r.measure = 'all')           AS rent_all,
           max(r.rental_price) FILTER (WHERE r.measure = 'one_bed')       AS rent_one_bed,
           max(r.rental_price) FILTER (WHERE r.measure = 'two_bed')       AS rent_two_bed,
           max(r.rental_price) FILTER (WHERE r.measure = 'three_bed')     AS rent_three_bed,
           max(r.rental_price) FILTER (WHERE r.measure = 'four_plus_bed') AS rent_four_plus_bed
      FROM production.london_rents r
      JOIN latest l USING (area_code, month)
     GROUP BY r.area_name, r.month
     ORDER BY r.area_name
    """
)

# The latest month's two-year fixed mortgage rates, one column per LTV band.
MORTGAGE_RATES_SQL = text(
    """
    SELECT rate_date, rate_2y_60ltv, rate_2y_75ltv, rate_2y_85ltv, rate_2y_90ltv, rate_2y_95ltv
      FROM production.mortgage_rates
     ORDER BY rate_date DESC
     LIMIT 1
    """
)

# Candidate nearby sales of the requested type or its PPD group (flats and
# maisonettes, or houses), sold since :date_from within :max_distance_km of the
# origin. Same type first, then closest floor area, then most recent.
# Over-fetched because streets are de-duplicated in Python.
COMPARABLES_SQL = text(
    f"""
    SELECT *
      FROM (
        SELECT h.ppd_id,
               h.price,
               h.dateoftransfer,
               h.total_floor_area,
               h.poststreet,
               h.latitude,
               h.longitude,
               {_TYPE_KEY} AS type_key,
               ({_TYPE_KEY}) = :property_type AS same_type,
               {_DISTANCE_KM} AS distance_km
          FROM production.london_houses h
         WHERE h.postcode_district = ANY(:members)
           AND h.dateoftransfer >= :date_from
           AND h.total_floor_area BETWEEN :min_floor_area AND :max_floor_area
           AND ({_TYPE_GROUP}) = :type_group
           AND nullif(trim(h.poststreet), '') IS NOT NULL
           AND h.latitude IS NOT NULL
           AND h.longitude IS NOT NULL
           -- The same plausibility window training applies (hpp_cb's
           -- PLAUSIBILITY_PRICE_PER_SQM_THRESHOLD, and MIN_PLAUSIBLE_PRICE).
           -- Bulk, part-share and other non-arms-length transfers are recorded
           -- at prices no comparable should carry: SW11 1AD holds a GBP 1.5m
           -- sale on a 96 sqm flat in a block that otherwise trades at
           -- GBP 355k-605k, and one such row sets the headline figure.
           AND h.price >= :min_price
           AND h.price / h.total_floor_area <= :max_price_per_sqm
      ) c
     WHERE c.distance_km <= :max_distance_km
     ORDER BY c.same_type DESC,
              abs(c.total_floor_area - :floor_area_sqm),
              c.dateoftransfer DESC,
              c.ppd_id DESC
     LIMIT :limit
    """
)

# The evidence scope's sales, and those of the same type within the requested
# floor-area band, over `districts.evidence_members`.
EVIDENCE_COUNTS_SQL = text(
    f"""
    SELECT count(*) AS sales_count,
           count(*) FILTER (
               WHERE ({_TYPE_KEY}) = :property_type
                 AND h.total_floor_area BETWEEN :floor_area_low AND :floor_area_high
           ) AS similar_count
      FROM production.london_houses h
     WHERE h.postcode_district = ANY(:members)
       AND h.dateoftransfer >= :date_from
    """
)

# Centre of an exact postcode's addresses; NULLs when the postcode is not held.
POSTCODE_CENTROID_SQL = text(
    """
    SELECT avg(c.latitude)  AS latitude,
           avg(c.longitude) AS longitude
      FROM production.address_table a
      JOIN production.address_coordinates c ON c.address_id = a.id
     WHERE a.postcode = :postcode
    """
)

# Median position of an outward code's sales; NULLs when it has none. Bound by
# the postcode_district index, unlike a LIKE over address_table.postcode.
OUTWARD_CENTROID_SQL = text(
    """
    SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY latitude)  AS latitude,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY longitude) AS longitude
      FROM production.london_houses
     WHERE postcode_district = :outward
       AND latitude IS NOT NULL
       AND longitude IS NOT NULL
    """
)
