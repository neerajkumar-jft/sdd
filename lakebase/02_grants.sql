-- =============================================================================
-- Grants for the Lakebase comments feature.
--
-- NOTE on section 2 below: superseded by sql/03_grant_persona.sh +
-- sql/04_comments_scoped_view.sql. Personas now read comments through
-- gold.v_comments_scoped (a per-viewer filtered view), not a direct grant on
-- pidilite_comments.public.comments - that base-table grant was a real
-- disclosure gap (any persona could read every comment, including ones about
-- dealers outside their own scope) and has been revoked. Kept here, struck
-- through in spirit, for the history of why the comments feature needs as
-- many distinct grants as it does.
--
-- Four identities/layers need access, for different reasons:
--
--  1. The comment app's own service principal - needs narrow SELECT on the
--     two access-map tables ONLY, to run its server-side authorization check
--     (guide section 11: "enforce the same access rules as the dashboard, so
--     users can only comment on data they can see"). This is not exposed to
--     end users - it is the app's own internal check, same reasoning as why
--     access_map_* is never granted to end users directly (sql/01_row_filters.sql).
--
--  2. [SUPERSEDED] Each real persona - see sql/03_grant_persona.sh instead,
--     which grants gold.v_comments_scoped and CAN_USE on both apps.
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
--
--  4. The scoped dashboard's Postgres role - needs SELECT, INSERT, UPDATE
--     (not just SELECT/INSERT like the comment app) for the "My Comments"
--     section, which lets a viewer edit their own past comments. Same
--     retroactive-grant gotcha as section 3: adding the database app
--     resource does not grant access to the pre-existing table.
-- =============================================================================

-- 1. Comment app's service principal (app-authorization check only)
-- Replace <APP_SERVICE_PRINCIPAL> with the app's service_principal_client_id
-- (from `databricks apps get pidilite-comments-app`).
GRANT USE CATALOG ON CATALOG pidilite_demo TO `<APP_SERVICE_PRINCIPAL>`;
GRANT USE SCHEMA ON SCHEMA pidilite_demo.gold TO `<APP_SERVICE_PRINCIPAL>`;
GRANT SELECT ON TABLE pidilite_demo.gold.access_map_customer TO `<APP_SERVICE_PRINCIPAL>`;
GRANT SELECT ON TABLE pidilite_demo.gold.access_map_field_team TO `<APP_SERVICE_PRINCIPAL>`;

-- 2. [SUPERSEDED - see sql/03_grant_persona.sh] Each real persona used to get
-- a direct grant here; now granted gold.v_comments_scoped instead.
-- GRANT SELECT ON TABLE pidilite_comments.public.comments TO `<persona>`;  -- do NOT run this

-- 3. Comment app's Postgres role - run via `databricks psql pidilite-comments`,
-- NOT the Unity Catalog SQL warehouse (that route is read-only federation).
-- Replace <APP_SERVICE_PRINCIPAL> with the same client id as in section 1 -
-- it is also the Postgres role name Databricks created for the app.
--   databricks psql pidilite-comments -- -d databricks_postgres -c \
--     'GRANT SELECT, INSERT ON public.comments TO "<APP_SERVICE_PRINCIPAL>";'

-- 4. Scoped dashboard's Postgres role - "My Comments" needs UPDATE too.
-- Replace <DASHBOARD_SERVICE_PRINCIPAL> with the scoped dashboard's
-- service_principal_client_id (from `databricks apps get pidilite-scoped-dashboard`).
--   databricks psql pidilite-comments -- -d databricks_postgres -c \
--     'GRANT SELECT, INSERT, UPDATE ON public.comments TO "<DASHBOARD_SERVICE_PRINCIPAL>";'
