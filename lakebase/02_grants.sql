-- =============================================================================
-- Grants for the Lakebase comments feature.
--
-- Three identities/layers need access, for different reasons:
--
--  1. The comment app's own service principal - needs narrow SELECT on the
--     two access-map tables ONLY, to run its server-side authorization check
--     (guide section 11: "enforce the same access rules as the dashboard, so
--     users can only comment on data they can see"). This is not exposed to
--     end users - it is the app's own internal check, same reasoning as why
--     access_map_* is never granted to end users directly (sql/01_row_filters.sql).
--
--  2. Each real persona - needs SELECT on pidilite_comments.public.comments so
--     the dashboard's "Recent Comments" widget resolves for them, and CAN_USE
--     on the comment app itself (an app-level permission, not a SQL grant -
--     see the `databricks permissions set apps` call in the README).
--
--  3. The comment app's Postgres role - needs a DIRECT Postgres GRANT on
--     public.comments. Adding a Lakebase database as an app resource only
--     grants CONNECT + CREATE on the *database* to the app's Postgres role
--     (name = the app's service_principal_client_id) - it does NOT grant
--     access to a table that already existed, created under a different
--     role (ours, via a direct psql session). Hit this as a real
--     InsufficientPrivilege error on the app's first INSERT; see section 3
--     below for the fix. USAGE on the `public` schema itself is already
--     granted to PUBLIC by default, so no separate schema grant is needed.
-- =============================================================================

-- 1. Comment app's service principal (app-authorization check only)
-- Replace <APP_SERVICE_PRINCIPAL> with the app's service_principal_client_id
-- (from `databricks apps get pidilite-comments-app`).
GRANT USE CATALOG ON CATALOG pidilite_demo TO `<APP_SERVICE_PRINCIPAL>`;
GRANT USE SCHEMA ON SCHEMA pidilite_demo.gold TO `<APP_SERVICE_PRINCIPAL>`;
GRANT SELECT ON TABLE pidilite_demo.gold.access_map_customer TO `<APP_SERVICE_PRINCIPAL>`;
GRANT SELECT ON TABLE pidilite_demo.gold.access_map_field_team TO `<APP_SERVICE_PRINCIPAL>`;

-- 2. Each real persona - read access to the comments table for the dashboard widget
-- Replace <persona> with each demo identity.
GRANT USE CATALOG ON CATALOG pidilite_comments TO `<persona>`;
GRANT USE SCHEMA ON SCHEMA pidilite_comments.public TO `<persona>`;
GRANT SELECT ON TABLE pidilite_comments.public.comments TO `<persona>`;

-- 3. Comment app's Postgres role - run via `databricks psql pidilite-comments`,
-- NOT the Unity Catalog SQL warehouse (that route is read-only federation).
-- Replace <APP_SERVICE_PRINCIPAL> with the same client id as in section 1 -
-- it is also the Postgres role name Databricks created for the app.
--   databricks psql pidilite-comments -- -d databricks_postgres -c \
--     'GRANT SELECT, INSERT ON public.comments TO "<APP_SERVICE_PRINCIPAL>";'
