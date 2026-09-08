-- =============================================================================
-- Lakebase (OLTP Postgres) comments table.
--
-- Run against the pidilite-comments Lakebase instance:
--   databricks psql pidilite-comments -- -d databricks_postgres -f 01_create_comments_table.sql
--
-- Why Postgres and not a Delta table: high-frequency, low-latency writes
-- (interactive commenting) are a poor fit for batch Delta tables - see the
-- build guide section 11.
--
-- Analytical-side access: once a catalog is registered over this instance
-- (`databricks database create-database-catalog pidilite_comments
--   pidilite-comments databricks_postgres --create-database-if-not-exists`),
-- this table is LIVE-federated through Unity Catalog as
-- pidilite_comments.public.comments - no separate sync job needed. Writing
-- here is queryable from the analytical side immediately, verified end to
-- end. That federation is read-only: writes must go through a direct
-- Postgres connection (the comment app), not the SQL warehouse.
-- =============================================================================

CREATE TABLE IF NOT EXISTS public.comments (
    comment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_type TEXT NOT NULL CHECK (scope_type IN ('customer', 'field_team', 'division')),
    scope_id TEXT NOT NULL,
    user_email TEXT NOT NULL,
    comment_text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULL until a comment is edited. Set by the scoped dashboard's "My
    -- Comments" editor (dashboard_app/app.py), the only place a comment can
    -- be updated after creation.
    updated_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_comments_scope ON public.comments (scope_type, scope_id);
-- "My Comments" filters by author - added when that feature landed.
CREATE INDEX IF NOT EXISTS idx_comments_user_email ON public.comments (user_email);
