-- =============================================================================
-- Row-level security for the X Industries capability demo.
--
-- Run AFTER the pipeline has published the gold layer at least once - the
-- filter functions reference gold.access_map_*, so those tables must exist.
--
-- Design notes worth keeping in mind while reading:
--
--  * One entitlement mechanism, not two. Head Office is a row in the access
--    map like everyone else, rather than a special case in the filter. That
--    keeps exactly one thing to reason about, and means Head Office rescopes
--    itself on a reorg like every other role. The group-membership variant
--    (which is what you want at production scale, where every HO user x every
--    customer is too many rows) is left below as a commented alternative.
--
--  * The caller does NOT need SELECT on access_map_*. A row filter function
--    reads its lookup table with the function's own privileges; the querying
--    user only needs EXECUTE on the function and SELECT on the base table.
--    So restricting the map is the default, not something to engineer - but
--    verify it (02_verify_rls.sql, check 3) rather than trusting it.
--
--  * Parameters are prefixed p_ so they cannot shadow the column of the same
--    name inside the lookup subquery.
-- =============================================================================

USE CATALOG pidilite_demo;


-- -----------------------------------------------------------------------------
-- 1. Filter functions
-- -----------------------------------------------------------------------------

-- Customer-grain entitlement. Backs the filter on fact_sales_transaction and
-- dim_customer. A cheap EXISTS against a pre-computed map, deliberately not a
-- recursive walk up the management chain on every query.
CREATE OR REPLACE FUNCTION pidilite_demo.gold.can_see_customer(p_customer_code INT)
RETURNS BOOLEAN
COMMENT 'Row filter: true when the current user is entitled to this customer.'
RETURN EXISTS (
  SELECT 1
  FROM pidilite_demo.gold.access_map_customer m
  WHERE lower(m.user_email) = lower(current_user())
    AND m.customer_code = p_customer_code
);

-- Territory-grain entitlement. Both columns are required: field_team_code
-- repeats across the Sales and MDI chains with different managers, so matching
-- on the code alone would hand one territory to two management lines.
CREATE OR REPLACE FUNCTION pidilite_demo.gold.can_see_field_team(
  p_field_team_code STRING,
  p_hierarchy_type  STRING
)
RETURNS BOOLEAN
COMMENT 'Row filter: true when the current user is entitled to this (field team, hierarchy) pair.'
RETURN EXISTS (
  SELECT 1
  FROM pidilite_demo.gold.access_map_field_team m
  WHERE lower(m.user_email) = lower(current_user())
    AND m.field_team_code = p_field_team_code
    AND m.hierarchy_type  = p_hierarchy_type
);


-- Production-scale alternative, kept for Phase 1, NOT active here.
-- Once the roster is org-wide, Head Office rows in the map multiply as
-- (HO users x customers) and it is cheaper to short-circuit on group
-- membership. Enabling this means Head Office is no longer represented in the
-- map, so the reorg-self-heal behaviour stops applying to HO - a deliberate
-- trade, not a free win.
--
-- CREATE OR REPLACE FUNCTION pidilite_demo.gold.can_see_customer(p_customer_code INT)
-- RETURNS BOOLEAN
-- RETURN IS_ACCOUNT_GROUP_MEMBER('head_office')
--     OR EXISTS (
--          SELECT 1 FROM pidilite_demo.gold.access_map_customer m
--          WHERE lower(m.user_email) = lower(current_user())
--            AND m.customer_code = p_customer_code
--        );


-- -----------------------------------------------------------------------------
-- 2. Attach the filters
--
-- Columns in the ON clause are passed positionally to the function, so the
-- order must match the parameter list.
--
-- These tables are LEAF nodes of the pipeline: nothing inside the pipeline
-- reads them downstream (gold.py reads silver, never gold). That is what stops
-- the pipeline's own service identity - which is not in the access map - from
-- being filtered to zero rows and silently publishing an empty table.
-- -----------------------------------------------------------------------------

ALTER TABLE pidilite_demo.gold.fact_sales_transaction
  SET ROW FILTER pidilite_demo.gold.can_see_customer ON (customer_code);

ALTER TABLE pidilite_demo.gold.dim_customer
  SET ROW FILTER pidilite_demo.gold.can_see_customer ON (customer_code);

ALTER TABLE pidilite_demo.gold.dim_field_team
  SET ROW FILTER pidilite_demo.gold.can_see_field_team ON (field_team_code, hierarchy_type);

-- Deliberately NOT filtered, and worth saying out loud rather than leaving
-- unexplained:
--   * gold.dim_person   - the internal org roster. Genie needs it to answer
--                         "who manages this territory", and an org chart is
--                         not normally treated as confidential per-territory.
--                         Confirm with the client; if they disagree, filter it
--                         on (field_team_code) via a person->team map.
--   * gold.dim_division - four rows, effectively reference data.
--   * gold.access_map_* - the entitlement tables themselves. Filtering these
--                         would be circular; they are protected by NOT being
--                         granted (section 3) instead.

-- Rollback, if a check fails and you need the tables back unfiltered:
--   ALTER TABLE pidilite_demo.gold.fact_sales_transaction DROP ROW FILTER;
--   ALTER TABLE pidilite_demo.gold.dim_customer          DROP ROW FILTER;
--   ALTER TABLE pidilite_demo.gold.dim_field_team        DROP ROW FILTER;


-- -----------------------------------------------------------------------------
-- 3. Grants
--
-- Table-level, NOT schema-level. `GRANT SELECT ON SCHEMA gold` would hand over
-- access_map_* as well, and then any user could read everyone else's
-- entitlements - and work out exactly what they are not allowed to see.
--
-- Replace `<persona>` with each demo identity. Run once per persona, or swap in
-- a group once the personas exist.
-- -----------------------------------------------------------------------------

-- GRANT USE CATALOG ON CATALOG pidilite_demo TO `<persona>`;
-- GRANT USE SCHEMA  ON SCHEMA  pidilite_demo.gold TO `<persona>`;
--
-- GRANT SELECT ON TABLE pidilite_demo.gold.fact_sales_transaction TO `<persona>`;
-- GRANT SELECT ON TABLE pidilite_demo.gold.dim_customer           TO `<persona>`;
-- GRANT SELECT ON TABLE pidilite_demo.gold.dim_field_team         TO `<persona>`;
-- GRANT SELECT ON TABLE pidilite_demo.gold.dim_person             TO `<persona>`;
-- GRANT SELECT ON TABLE pidilite_demo.gold.dim_division           TO `<persona>`;
--
-- GRANT EXECUTE ON FUNCTION pidilite_demo.gold.can_see_customer   TO `<persona>`;
-- GRANT EXECUTE ON FUNCTION pidilite_demo.gold.can_see_field_team TO `<persona>`;
--
-- Explicitly withheld - do not grant, and revoke if inherited:
-- REVOKE ALL PRIVILEGES ON TABLE pidilite_demo.gold.access_map_customer   FROM `<persona>`;
-- REVOKE ALL PRIVILEGES ON TABLE pidilite_demo.gold.access_map_field_team FROM `<persona>`;
