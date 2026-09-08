-- =============================================================================
-- A scope-filtered view over the Lakebase comments table.
--
-- Why this is needed: pidilite_comments.public.comments is a FEDERATED table
-- over Lakebase Postgres, so it carries no Unity Catalog row filter - and the
-- personas were granted SELECT on it directly. That made the comment *write*
-- path governed (the comment app checks access_map_* server-side before
-- allowing a comment) while leaving the *read* path wide open: any persona
-- could read every comment, including comments about dealers outside their
-- own scope. Someone else's note on a dealer you cannot see is itself a
-- disclosure - it tells you the dealer exists and what is happening with it.
--
-- The fix is the same pattern used everywhere else here: read through a view
-- that resolves current_user() against the access maps, grant the view, and
-- revoke the base table. The view reads the base table with its owner's
-- privileges while current_user() still evaluates to the caller, so it filters
-- per-viewer without the caller needing any access of their own.
--
-- Run after 01_row_filters.sql, and after the Lakebase catalog exists.
-- =============================================================================

USE CATALOG pidilite_demo;

CREATE OR REPLACE VIEW pidilite_demo.gold.v_comments_scoped (
  comment_id   COMMENT 'Unique identifier for the comment.',
  scope_type   COMMENT 'What the comment is attached to: customer, field_team or division.',
  scope_id     COMMENT 'Identifier of the row being commented on, as text.',
  author_email COMMENT 'Who wrote the comment.',
  comment_text COMMENT 'The comment itself.',
  created_at   COMMENT 'When it was written.'
)
COMMENT 'Comments on dealers and territories, filtered to the caller''s own scope. Read this rather than pidilite_comments.public.comments, which is unfiltered.'
AS
SELECT
  c.comment_id,
  c.scope_type,
  c.scope_id,
  c.user_email AS author_email,
  c.comment_text,
  c.created_at
FROM pidilite_comments.public.comments c
WHERE
  -- Dealer-scoped: the caller must be entitled to that dealer.
  (
    c.scope_type = 'customer'
    AND EXISTS (
      SELECT 1
      FROM pidilite_demo.gold.access_map_customer m
      WHERE lower(m.user_email) = lower(current_user())
        AND cast(m.customer_code AS STRING) = c.scope_id
    )
  )
  OR
  -- Territory-scoped. NOTE a known imprecision: the comments table stores only
  -- scope_id, with no hierarchy_type, so a territory comment can only be
  -- matched on the code. A caller entitled to WSSTTY3 in one chain therefore
  -- also sees territory comments written against WSSTTY3 in the other. That is
  -- the same code-alone ambiguity the dimensional model was fixed for, and the
  -- proper fix is a hierarchy_type column on the comments table - the comment
  -- app already receives it in the URL, it just is not stored.
  (
    c.scope_type = 'field_team'
    AND EXISTS (
      SELECT 1
      FROM pidilite_demo.gold.access_map_field_team m
      WHERE lower(m.user_email) = lower(current_user())
        AND m.field_team_code = c.scope_id
    )
  )
  OR
  -- Division-scoped: deliberately restricted to Head Office. Neither app
  -- writes this scope today, so the conservative rule costs nothing; widen it
  -- when something actually produces division-level comments.
  (
    c.scope_type = 'division'
    AND EXISTS (
      SELECT 1
      FROM pidilite_demo.gold.dim_person p
      WHERE lower(p.user_email) = lower(current_user())
        AND p.role = 'Head Office'
    )
  );


-- -----------------------------------------------------------------------------
-- Grants. The view replaces direct access to the base table - granting both
-- would defeat the point, so the base-table grant is revoked.
--
-- 03_grant_persona.sh does both of these for a persona; the statements are
-- here for the record and for principals the script does not cover.
-- -----------------------------------------------------------------------------

-- GRANT SELECT ON VIEW pidilite_demo.gold.v_comments_scoped TO `<persona>`;
-- REVOKE SELECT ON TABLE pidilite_comments.public.comments FROM `<persona>`;

-- The comment app's own service principal keeps its direct grants: it needs to
-- INSERT through Postgres and to run its own authorization check, and it is
-- not an end-user surface.
