"""
The EPC to London-address derivation and the EPC/PPD insert SQL.

Executed by migration 0006.
The ``ON CONFLICT`` and ``WHERE NOT EXISTS`` guards make it re-runnable after
a failure. It builds three staging scratch tables: ``_epc_london_addr`` (one
row per London EPC UPRN with the address split), ``_epc_london_addrid``
(joined to ``address_table.id``) and ``_epc_london_pick`` (one certificate per
address and inspection date, newest lodgement winning).
"""

ADDRESS_DERIVATION_STATEMENTS = (
    "DROP TABLE IF EXISTS staging._epc_london_addr",
    """
    CREATE TABLE staging._epc_london_addr AS
    WITH rep AS (
        SELECT DISTINCT ON (e.uprn)
               e.uprn,
               btrim(coalesce(e.address1,'')) AS a1,
               btrim(coalesce(e.address2,'')) AS a2,
               btrim(coalesce(e.address3,'')) AS a3,
               btrim(coalesce(nullif(btrim(e.posttown),''), e.local_authority_label, 'LONDON')) AS town,
               btrim(e.postcode) AS postcode
        FROM staging.epc_domestic e
        WHERE e.region = 'E12000007' AND e.uprn IS NOT NULL
        ORDER BY e.uprn, e.lodgement_datetime DESC NULLS LAST, e.certificate_number DESC
    ),
    prem AS (
        SELECT rep.*,
               -- first address line that starts with a house number
               CASE WHEN a1 ~ '^[0-9]' THEN a1
                    WHEN a2 ~ '^[0-9]' THEN a2
                    WHEN a3 ~ '^[0-9]' THEN a3
               END AS premise_line
        FROM rep
    )
    SELECT
        uprn,
        upper(CASE
            WHEN premise_line IS NOT NULL
                THEN substring(premise_line from '^([0-9]+[A-Za-z]?)')
            -- no numbered line: a1 is a flat/floor descriptor, so the building name is on a2
            WHEN a1 ~* '^(flat|apartment|apt|unit|ground|first|second|third|fourth|fifth|top|basement|lower|upper|rear|front|maisonette|studio|room|the flat)\\M'
                 AND a2 <> '' THEN a2
            WHEN a1 <> '' THEN a1
            ELSE coalesce(nullif(a2,''), nullif(a3,''), postcode)
        END) AS postnumber,
        nullif(upper(CASE
            WHEN premise_line IS NOT NULL
                THEN btrim(regexp_replace(premise_line, '^[0-9]+[A-Za-z]?[[:space:],]*', ''))
            WHEN a1 ~* '^(flat|apartment|apt|unit|ground|first|second|third|fourth|fifth|top|basement|lower|upper|rear|front|maisonette|studio|room|the flat)\\M'
                 AND a2 <> '' THEN a3
            ELSE a2
        END), '') AS poststreet,
        upper(town) AS posttown,
        postcode,
        'GREATER LONDON'::varchar AS postcounty
    FROM prem
    """,
    "CREATE UNIQUE INDEX ON staging._epc_london_addr (uprn)",
    "ANALYZE staging._epc_london_addr",
)

EPC_DEDUPE_STATEMENTS = (
    "DROP TABLE IF EXISTS staging._epc_london_addrid",
    """
    CREATE TABLE staging._epc_london_addrid AS
    SELECT la.uprn, a.id AS address_id
    FROM staging._epc_london_addr la
    JOIN staging.ukhcs_table k ON k."UPRN" = la.uprn
    JOIN staging.address_table a ON a.uid = k.id
    """,
    "CREATE UNIQUE INDEX ON staging._epc_london_addrid (uprn)",
    "ANALYZE staging._epc_london_addrid",
    "DROP TABLE IF EXISTS staging._epc_london_pick",
    """
    CREATE TABLE staging._epc_london_pick AS
    SELECT DISTINCT ON (m.address_id, e.inspection_date) m.address_id, e.*
    FROM staging.epc_domestic e
    JOIN staging._epc_london_addrid m ON m.uprn = e.uprn
    WHERE e.region = 'E12000007'
    ORDER BY m.address_id, e.inspection_date,
             e.lodgement_datetime DESC NULLS LAST, e.certificate_number DESC
    """,
    "ANALYZE staging._epc_london_pick",
)


SI_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION pg_temp.si(numeric) RETURNS smallint
LANGUAGE sql IMMUTABLE AS $$ SELECT CASE WHEN $1 BETWEEN -32768 AND 32767 THEN round($1)::smallint END $$
"""

EPC_TABLE_INSERT_SQL = """
INSERT INTO staging.epc_table (
    address_id, current_energy_rating, potential_energy_rating,
    current_energy_efficiency, potential_energy_efficiency, build_form,
    inspection_date, transaction_type, environment_impact_current,
    environment_impact_potential, energy_consumption_current,
    energy_consumption_potential, co2_emissions_current,
    co2_emiss_curr_per_floor_area, co2_emissions_potential,
    lighting_cost_current, lighting_cost_potential, heating_cost_current,
    heating_cost_potential, hot_water_cost_current, hot_water_cost_potential,
    total_floor_area, energy_tariff, mains_gas_flag, flat_storey_count,
    multi_glaze_proportion, glazed_type, extension_count,
    number_habitable_rooms, number_heated_rooms, low_energy_lighting,
    hot_water_energy_eff, hot_water_env_eff, windows_energy_eff,
    windows_env_eff, walls_energy_eff, walls_env_eff, roof_energy_eff,
    roof_env_eff, mainheat_energy_eff, mainheat_env_eff,
    mainheatc_energy_eff, mainheatc_env_eff, lighting_energy_eff,
    lighting_env_eff, construction_age_band, lodgement_datetime, tenure
)
SELECT
    p.address_id,
    CASE WHEN upper(btrim(p.current_energy_rating))   ~ '^[A-G]$' THEN upper(btrim(p.current_energy_rating))::bpchar END,
    CASE WHEN upper(btrim(p.potential_energy_rating)) ~ '^[A-G]$' THEN upper(btrim(p.potential_energy_rating))::bpchar END,
    pg_temp.si(p.current_energy_efficiency), pg_temp.si(p.potential_energy_efficiency),
    bf.id,
    p.inspection_date,
    tt.id,
    pg_temp.si(p.environment_impact_current), pg_temp.si(p.environment_impact_potential),
    pg_temp.si(p.energy_consumption_current), pg_temp.si(p.energy_consumption_potential),
    p.co2_emissions_current, pg_temp.si(p.co2_emiss_curr_per_floor_area), p.co2_emissions_potential,
    pg_temp.si(p.lighting_cost_current), pg_temp.si(p.lighting_cost_potential),
    pg_temp.si(p.heating_cost_current),  pg_temp.si(p.heating_cost_potential),
    pg_temp.si(p.hot_water_cost_current),pg_temp.si(p.hot_water_cost_potential),
    p.total_floor_area,
    et.id,
    CASE WHEN upper(btrim(p.mains_gas_flag)) IN ('Y','N') THEN upper(btrim(p.mains_gas_flag))::bpchar END,
    pg_temp.si(p.flat_storey_count), pg_temp.si(p.multi_glaze_proportion),
    gt.id,
    pg_temp.si(p.extension_count), pg_temp.si(p.number_habitable_rooms),
    pg_temp.si(p.number_heated_rooms), pg_temp.si(p.low_energy_lighting),
    r_hwe.id, r_hwv.id, r_wine.id, r_winv.id, r_wale.id, r_walv.id,
    r_roofe.id, r_roofv.id, r_mhe.id, r_mhv.id, r_mhce.id, r_mhcv.id,
    r_lite.id, r_litv.id,
    nullif(btrim(p.construction_age_band), ''),
    p.lodgement_datetime,
    CASE lower(btrim(p.tenure))
        WHEN 'owner-occupied'   THEN 'O'::bpchar
        WHEN 'rented (private)' THEN 'P'::bpchar
        WHEN 'rental (private)' THEN 'P'::bpchar
        WHEN 'rented (social)'  THEN 'S'::bpchar
        WHEN 'rental (social)'  THEN 'S'::bpchar
    END
FROM staging._epc_london_pick p
LEFT JOIN staging.build_form_category_table       bf ON lower(bf.build_form)       = lower(btrim(p.built_form))
LEFT JOIN staging.energy_tariff_category_table    et ON lower(et.energy_tariff)    = lower(btrim(p.energy_tariff))
LEFT JOIN staging.glazed_type_category_table      gt ON lower(gt.glazed_type)      = lower(btrim(p.glazed_type))
LEFT JOIN staging.transaction_type_category_table tt
       ON replace(lower(tt.transaction_type),'-',' ') = replace(lower(btrim(p.transaction_type)),'-',' ')
LEFT JOIN staging.rating_category_table r_hwe   ON lower(r_hwe.rating_category)   = lower(btrim(p.hot_water_energy_eff))
LEFT JOIN staging.rating_category_table r_hwv   ON lower(r_hwv.rating_category)   = lower(btrim(p.hot_water_env_eff))
LEFT JOIN staging.rating_category_table r_wine  ON lower(r_wine.rating_category)  = lower(btrim(p.windows_energy_eff))
LEFT JOIN staging.rating_category_table r_winv  ON lower(r_winv.rating_category)  = lower(btrim(p.windows_env_eff))
LEFT JOIN staging.rating_category_table r_wale  ON lower(r_wale.rating_category)  = lower(btrim(p.walls_energy_eff))
LEFT JOIN staging.rating_category_table r_walv  ON lower(r_walv.rating_category)  = lower(btrim(p.walls_env_eff))
LEFT JOIN staging.rating_category_table r_roofe ON lower(r_roofe.rating_category) = lower(btrim(p.roof_energy_eff))
LEFT JOIN staging.rating_category_table r_roofv ON lower(r_roofv.rating_category) = lower(btrim(p.roof_env_eff))
LEFT JOIN staging.rating_category_table r_mhe   ON lower(r_mhe.rating_category)   = lower(btrim(p.mainheat_energy_eff))
LEFT JOIN staging.rating_category_table r_mhv   ON lower(r_mhv.rating_category)   = lower(btrim(p.mainheat_env_eff))
LEFT JOIN staging.rating_category_table r_mhce  ON lower(r_mhce.rating_category)  = lower(btrim(p.mainheatc_energy_eff))
LEFT JOIN staging.rating_category_table r_mhcv  ON lower(r_mhcv.rating_category)  = lower(btrim(p.mainheatc_env_eff))
LEFT JOIN staging.rating_category_table r_lite  ON lower(r_lite.rating_category)  = lower(btrim(p.lighting_energy_eff))
LEFT JOIN staging.rating_category_table r_litv  ON lower(r_litv.rating_category)  = lower(btrim(p.lighting_env_eff))
ON CONFLICT ON CONSTRAINT uq_epc_table_address_inspection DO NOTHING
"""

EPC_CONDENSED_INSERT_SQL = """
INSERT INTO staging.epc_table_condensed (
    uprn, current_energy_efficiency, potential_energy_efficiency, transaction_type,
    environment_impact_current, environment_impact_potential,
    energy_consumption_current, energy_consumption_potential,
    co2_emissions_current, co2_emiss_curr_per_floor_area, co2_emissions_potential,
    energy_tariff, mains_gas_flag, multi_glaze_proportion, extension_count,
    number_habitable_rooms, number_heated_rooms, low_energy_lighting,
    hot_water_energy_eff, hot_water_env_eff, windows_energy_eff, windows_env_eff,
    walls_energy_eff, walls_env_eff, roof_energy_eff, roof_env_eff,
    mainheat_energy_eff, mainheat_env_eff, mainheatc_energy_eff, mainheatc_env_eff,
    lighting_energy_eff, lighting_env_eff, lodgement_datetime, glazed_type,
    inspection_date, build_form
)
SELECT
    p.uprn,
    round(p.current_energy_efficiency)::int, round(p.potential_energy_efficiency)::int,
    tt.transaction_type,
    round(p.environment_impact_current)::int, round(p.environment_impact_potential)::int,
    round(p.energy_consumption_current)::int, round(p.energy_consumption_potential)::int,
    p.co2_emissions_current, round(p.co2_emiss_curr_per_floor_area)::int, p.co2_emissions_potential,
    et.energy_tariff,
    CASE WHEN upper(btrim(p.mains_gas_flag)) IN ('Y','N') THEN upper(btrim(p.mains_gas_flag)) END,
    round(p.multi_glaze_proportion)::int, round(p.extension_count)::int,
    round(p.number_habitable_rooms)::int, round(p.number_heated_rooms)::int,
    round(p.low_energy_lighting)::int,
    r_hwe.rating_category,  r_hwv.rating_category,
    r_wine.rating_category, r_winv.rating_category,
    r_wale.rating_category, r_walv.rating_category,
    r_roofe.rating_category,r_roofv.rating_category,
    r_mhe.rating_category,  r_mhv.rating_category,
    r_mhce.rating_category, r_mhcv.rating_category,
    r_lite.rating_category, r_litv.rating_category,
    p.lodgement_datetime,
    gt.glazed_type,
    p.inspection_date,
    bf.build_form
FROM staging._epc_london_pick p
LEFT JOIN staging.build_form_category_table       bf ON lower(bf.build_form)       = lower(btrim(p.built_form))
LEFT JOIN staging.energy_tariff_category_table    et ON lower(et.energy_tariff)    = lower(btrim(p.energy_tariff))
LEFT JOIN staging.glazed_type_category_table      gt ON lower(gt.glazed_type)      = lower(btrim(p.glazed_type))
LEFT JOIN staging.transaction_type_category_table tt
       ON replace(lower(tt.transaction_type),'-',' ') = replace(lower(btrim(p.transaction_type)),'-',' ')
LEFT JOIN staging.rating_category_table r_hwe   ON lower(r_hwe.rating_category)   = lower(btrim(p.hot_water_energy_eff))
LEFT JOIN staging.rating_category_table r_hwv   ON lower(r_hwv.rating_category)   = lower(btrim(p.hot_water_env_eff))
LEFT JOIN staging.rating_category_table r_wine  ON lower(r_wine.rating_category)  = lower(btrim(p.windows_energy_eff))
LEFT JOIN staging.rating_category_table r_winv  ON lower(r_winv.rating_category)  = lower(btrim(p.windows_env_eff))
LEFT JOIN staging.rating_category_table r_wale  ON lower(r_wale.rating_category)  = lower(btrim(p.walls_energy_eff))
LEFT JOIN staging.rating_category_table r_walv  ON lower(r_walv.rating_category)  = lower(btrim(p.walls_env_eff))
LEFT JOIN staging.rating_category_table r_roofe ON lower(r_roofe.rating_category) = lower(btrim(p.roof_energy_eff))
LEFT JOIN staging.rating_category_table r_roofv ON lower(r_roofv.rating_category) = lower(btrim(p.roof_env_eff))
LEFT JOIN staging.rating_category_table r_mhe   ON lower(r_mhe.rating_category)   = lower(btrim(p.mainheat_energy_eff))
LEFT JOIN staging.rating_category_table r_mhv   ON lower(r_mhv.rating_category)   = lower(btrim(p.mainheat_env_eff))
LEFT JOIN staging.rating_category_table r_mhce  ON lower(r_mhce.rating_category)  = lower(btrim(p.mainheatc_energy_eff))
LEFT JOIN staging.rating_category_table r_mhcv  ON lower(r_mhcv.rating_category)  = lower(btrim(p.mainheatc_env_eff))
LEFT JOIN staging.rating_category_table r_lite  ON lower(r_lite.rating_category)  = lower(btrim(p.lighting_energy_eff))
LEFT JOIN staging.rating_category_table r_litv  ON lower(r_litv.rating_category)  = lower(btrim(p.lighting_env_eff))
WHERE NOT EXISTS (
    SELECT 1 FROM staging.epc_table_condensed c
    WHERE c.uprn = p.uprn AND c.inspection_date IS NOT DISTINCT FROM p.inspection_date
)
"""


# Mint address rows for UPRNs that appear only in the Tier A fixture, which no
# other step creates on a from-empty build; newest certificate with a postcode.
TIERA_ADDRESS_MINT_SQL = """
INSERT INTO staging.address_table
    (uid, postnumber, poststreet, posttown, postcode, postcounty)
SELECT DISTINCT ON (u.id)
    u.id AS uid,
    COALESCE(NULLIF(btrim(e.address1), ''), 'UNKNOWN') AS postnumber,
    NULLIF(btrim(e.address2), '') AS poststreet,
    COALESCE(
        NULLIF(btrim(e.posttown), ''), e.local_authority_label, 'LONDON'
    ) AS posttown,
    btrim(e.postcode) AS postcode,
    'GREATER LONDON'::varchar AS postcounty
FROM (
    SELECT DISTINCT l.uprn
    FROM staging._ppd_uprn_linkage_tiera l
    JOIN staging.ukhcs_table uk ON uk."UPRN" = l.uprn
    LEFT JOIN staging.address_table a ON a.uid = uk.id
    WHERE a.id IS NULL
) unresolved
JOIN staging.ukhcs_table u ON u."UPRN" = unresolved.uprn
JOIN staging.epc_domestic_history e ON e.uprn = unresolved.uprn
WHERE NULLIF(btrim(e.postcode), '') IS NOT NULL
ORDER BY u.id, e.lodgement_datetime DESC NULLS LAST,
         e.certificate_number DESC
ON CONFLICT (uid) DO NOTHING
"""


# Resolve the fixture's links through its stable UPRN, not its frozen
# address_id, which does not match ids minted on a from-empty build.
TIERA_PPD_TABLE_INSERT_SQL = """
INSERT INTO staging.ppd_table (address_id, price, dateoftransfer, propertytype, duration)
SELECT a.id, l.price, l.dateoftransfer, l.propertytype::bpchar, l.duration::bpchar
FROM staging._ppd_uprn_linkage_tiera l
JOIN staging.ukhcs_table u ON u."UPRN" = l.uprn
JOIN staging.address_table a ON a.uid = u.id
ON CONFLICT ON CONSTRAINT uq_ppd_table_transaction DO NOTHING
"""
