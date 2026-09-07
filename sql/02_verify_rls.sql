-- =============================================================================
-- RLS verification for the X Industries capability demo.
--
-- Run in order. Checks 1-4 are the ones that decide whether the demo's
-- centrepiece actually holds; run them BEFORE building the dashboard, so a
-- permission bug can never be mistaken for a dashboard bug.
--
-- tests/verify_access_map_logic.py already proves the entitlement *algebra*
-- offline (containment, no cross-chain leak, coverage). It cannot prove any of
-- what follows: that Unity Catalog enforces it, that the filter survives a
-- pipeline update, or that the pipeline's own identity is not caught by it.
-- =============================================================================

USE CATALOG pidilite_demo;


-- -----------------------------------------------------------------------------
-- CHECK 0 - baseline: what SHOULD each persona see?
--
-- Run as an admin (who is not filtered by the map, because an admin is not in
-- it - see the note at the bottom). Gives you the expected row counts before
-- you go and test as each persona, so "is 47 correct?" has an answer.
-- -----------------------------------------------------------------------------

SELECT
  p.role,
  p.hierarchy_type,
  p.person_name,
  p.user_email,
  count(DISTINCT m.customer_code) AS customers_expected
FROM pidilite_demo.gold.dim_person p
LEFT JOIN pidilite_demo.gold.access_map_customer m
       ON lower(m.user_email) = lower(p.user_email)
GROUP BY ALL
ORDER BY customers_expected DESC, p.role, p.person_name;

-- Containment must hold: a Master's count <= their RA1's <= their RA2's <=
-- Head Office's. If it does not, the map is wrong and nothing downstream is
-- worth testing yet.


-- -----------------------------------------------------------------------------
-- CHECK 1 - is the filter actually attached?
-- -----------------------------------------------------------------------------

DESCRIBE TABLE EXTENDED pidilite_demo.gold.fact_sales_transaction;
DESCRIBE TABLE EXTENDED pidilite_demo.gold.dim_customer;
DESCRIBE TABLE EXTENDED pidilite_demo.gold.dim_field_team;

-- Catalog-wide view of every filter in place. Inspect the column names on
-- first run rather than assuming them - this view's shape is not something to
-- guess at.
SELECT * FROM pidilite_demo.information_schema.row_filters;


-- -----------------------------------------------------------------------------
-- CHECK 2 - does the filter SURVIVE a pipeline update?
--
-- This is the load-bearing question for the whole architecture, because the
-- filter is attached by an external ALTER while Lakeflow owns these tables.
--
--   1. run CHECK 1 and note what is attached
--   2. databricks bundle deploy --profile pidilite -t dev
--   3. databricks bundle run pidilite_demo_pipeline --profile pidilite -t dev
--   4. re-run CHECK 1
--
-- Still attached  -> the external-ALTER approach is fine; keep it.
-- Gone            -> declare the filter inside the pipeline's own table
--                    definition instead (SQL `CREATE OR REFRESH ... WITH ROW
--                    FILTER`, added as a pipeline library), or move the filters
--                    onto consumption views layered over gold. Do NOT paper
--                    over it by re-running this script after every deploy -
--                    that is a live demo waiting to fail.
-- -----------------------------------------------------------------------------


-- -----------------------------------------------------------------------------
-- CHECK 3 - does the filter work for a user with NO access to the map?
--
-- A row filter function should read its lookup table with the function's own
-- privileges, so the caller needs EXECUTE on the function and SELECT on the
-- base table, but not SELECT on access_map_*. Prove it, do not assume it.
--
-- As the persona:
--     SELECT count(*) FROM pidilite_demo.gold.fact_sales_transaction;
--       -> expect their own scoped count, NOT an error
--
--     SELECT count(*) FROM pidilite_demo.gold.access_map_customer;
--       -> expect PERMISSION DENIED
--
-- If the first query fails, the function is not reading with definer rights and
-- the grants need revisiting. If the second query SUCCEEDS, every user can read
-- everyone's entitlements - fix the grants before going anywhere near a demo.
-- -----------------------------------------------------------------------------


-- -----------------------------------------------------------------------------
-- CHECK 4 - does a FULL REFRESH leave the gold tables intact?
--
-- The failure being tested for is silent: the pipeline runs as its own service
-- identity, that identity is not in the access map, so if it ever reads a
-- filtered gold table it sees zero rows and cheerfully publishes an empty
-- table. No error, no warning - just an empty dashboard.
--
-- gold.py is written to make this impossible (every read comes from silver,
-- gold tables are leaves), but that is a claim about the code, so test it:
--
--     databricks bundle run pidilite_demo_pipeline --full-refresh-all \
--       --profile pidilite -t dev
--
-- then, as an admin:
-- -----------------------------------------------------------------------------

SELECT 'gold.dim_division'            AS tbl, count(*) AS n FROM pidilite_demo.gold.dim_division
UNION ALL SELECT 'gold.dim_person',            count(*) FROM pidilite_demo.gold.dim_person
UNION ALL SELECT 'gold.dim_field_team',        count(*) FROM pidilite_demo.gold.dim_field_team
UNION ALL SELECT 'gold.dim_customer',          count(*) FROM pidilite_demo.gold.dim_customer
UNION ALL SELECT 'gold.fact_sales_transaction',count(*) FROM pidilite_demo.gold.fact_sales_transaction
UNION ALL SELECT 'gold.access_map_customer',   count(*) FROM pidilite_demo.gold.access_map_customer
UNION ALL SELECT 'gold.access_map_field_team', count(*) FROM pidilite_demo.gold.access_map_field_team
ORDER BY tbl;

-- Expected, from the current generated seed (tests/verify_access_map_logic.py):
--   dim_division                4
--   dim_person                 30      (32 generated, 2 quarantined)
--   dim_field_team             17      (18 generated, 1 quarantined)
--   dim_customer              121      (123 generated, 2 quarantined)
--   fact_sales_transaction   3662      (3666 generated, 4 quarantined)
--   access_map_field_team     119
--   access_map_customer       847
-- A zero anywhere here is the failure this check exists to catch.


-- -----------------------------------------------------------------------------
-- CHECK 5 - the data-quality story (also a demo moment)
--
-- The quarantine tables must be non-empty, or there is nothing to show for the
-- cleansing half of the pitch. Each row carries its own named reason.
-- -----------------------------------------------------------------------------

SELECT 'dim_person'  AS entity, _quarantine_reasons, count(*) AS n
  FROM pidilite_demo.silver.dim_person_quarantine  GROUP BY ALL
UNION ALL
SELECT 'dim_field_team', _quarantine_reasons, count(*)
  FROM pidilite_demo.silver.dim_field_team_quarantine GROUP BY ALL
UNION ALL
SELECT 'dim_customer', _quarantine_reasons, count(*)
  FROM pidilite_demo.silver.dim_customer_quarantine GROUP BY ALL
UNION ALL
SELECT 'fact_sales_transaction', _quarantine_reasons, count(*)
  FROM pidilite_demo.silver.fact_sales_transaction_quarantine GROUP BY ALL
ORDER BY entity;

-- Also worth showing: the client's own column typo being repaired.
-- bronze.raw_field_team has a `Tzxntyoe` column; silver.dim_field_team has
-- `hierarchy_type`, same data.
DESCRIBE TABLE pidilite_demo.bronze.raw_field_team;
DESCRIBE TABLE pidilite_demo.silver.dim_field_team;


-- =============================================================================
-- DEMO QUERIES - not verification, but the moments worth rehearsing
-- =============================================================================

-- -----------------------------------------------------------------------------
-- "Who can see this row, and in what capacity?"
--
-- The reverse of the filter. It is an audit answer, and it is the most
-- convincing way to show the entitlement is real rather than asserted - most
-- demos claim the security works, this one shows you its contents.
-- Run as an admin; swap in any customer_code.
-- -----------------------------------------------------------------------------

SELECT
  c.customer_code,
  c.customer_name,
  c.field_team_code,
  c.hierarchy_type,
  m.via_role,
  p.person_name,
  m.user_email
FROM pidilite_demo.gold.access_map_customer m
JOIN pidilite_demo.gold.dim_customer c USING (customer_code)
JOIN pidilite_demo.gold.dim_person   p ON p.person_id = m.person_id
WHERE c.customer_code = 50
ORDER BY CASE m.via_role
           WHEN 'Territory/Area Sales Manager'  THEN 1
           WHEN 'Regional/Zonal Sales Manager'  THEN 2
           WHEN 'National Sales Manager'        THEN 3
           ELSE 4
         END;


-- -----------------------------------------------------------------------------
-- "Try to break it" - hand the client the keyboard.
--
-- As a Territory Manager persona, ask for a customer that belongs to a
-- different territory. It returns zero rows, not an error: the row does not
-- exist for that user. Then run the identical query as Head Office and the
-- data appears. Competitors will not offer this because they are afraid the
-- demo breaks.
-- -----------------------------------------------------------------------------

-- as the persona:      SELECT * FROM pidilite_demo.gold.dim_customer WHERE customer_code = 50;
-- as Head Office:      SELECT * FROM pidilite_demo.gold.dim_customer WHERE customer_code = 50;


-- -----------------------------------------------------------------------------
-- "Permissions that heal themselves" - the differentiator competitors cannot
-- demo, because a hand-maintained mapping table cannot do this.
--
--   1. note a Master's scope:
--        SELECT count(*) FROM pidilite_demo.gold.access_map_customer
--        WHERE lower(user_email) = 'viraj.tiwari@salesdemo.com';
--   2. promote them in the source: edit their role in
--      sample_data/person/person.csv (or reassign a territory in
--      field_team.csv), re-land the CSV, re-run the pipeline
--   3. re-run the count - the scope has changed
--
-- No permission code was touched, no ticket was raised. The map is derived
-- from the org chart, so the org chart IS the permission model.
-- -----------------------------------------------------------------------------


-- =============================================================================
-- A note on testing as other users
--
-- Every check above that says "as the persona" needs an identity the workspace
-- can actually authenticate. The generated roster uses @salesdemo.com
-- addresses, which are not real workspace users - nobody can log in as them,
-- so current_user() will never return one. Until that is resolved, these run
-- via Unity Catalog's query-as-another-user path only, and the live
-- persona-switching dashboard walkthrough is not yet demonstrable.
-- =============================================================================
