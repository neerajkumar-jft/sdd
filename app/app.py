"""
X Industries — Add a Comment.

Writes to the Lakebase (OLTP Postgres) `comments` table. Reads (for the
dashboard and for the "recent comments" list below) go through the UC
catalog `pidilite_comments`, which live-federates the same Postgres table -
no separate sync job, a write here is queryable from the analytical side
immediately.

Access control mirrors the dashboard's row-level security: before allowing a
comment, this checks pidilite_demo.gold.access_map_customer /
access_map_field_team for the *commenting* user, using the app's own service
principal (granted narrow SELECT on just those two tables - not exposed to
end users, this is a server-side check only).

Reached two ways:
  - via a row link from the dashboard (?scope_type=...&scope_id=...&...) -
    the record is pre-filled, no id to remember or type
  - opened directly, with no query params - falls back to a manual form
"""
import os

import psycopg2
import streamlit as st
from databricks.sdk import WorkspaceClient

INSTANCE_NAME = "pidilite-comments"
WAREHOUSE_ID = "be5dd2cb70eb66ee"

st.set_page_config(page_title="X Industries — Add a Comment", layout="centered")
st.title("Add a Comment")
st.caption(
    "Comments are scoped to a customer or field team. You can only comment on "
    "records you're entitled to see - the same rule the dashboard's row "
    "filters enforce."
)

w = WorkspaceClient()


def viewer_email() -> str:
    """The logged-in viewer's email, forwarded by Databricks Apps.
    Falls back to a manual field only when no forwarded identity is present
    (e.g. local dev) - never trust that fallback in a real deployment."""
    email = st.context.headers.get("X-Forwarded-Email")
    if email:
        return email
    return st.text_input("Your email (dev fallback — no forwarded identity found)")


def run_sql(statement: str, parameters: list | None = None) -> list:
    resp = w.statement_execution.execute_statement(
        warehouse_id=WAREHOUSE_ID,
        statement=statement,
        wait_timeout="30s",
        parameters=parameters or [],
    )
    if not resp.result or not resp.result.data_array:
        return []
    return resp.result.data_array


def is_authorized(email: str, scope_type: str, scope_id: str, hierarchy_type: str | None) -> bool:
    if scope_type == "customer":
        stmt = (
            "SELECT 1 FROM pidilite_demo.gold.access_map_customer "
            "WHERE lower(user_email) = lower(:email) AND customer_code = :code LIMIT 1"
        )
        params = [
            {"name": "email", "value": email},
            {"name": "code", "value": scope_id, "type": "INT"},
        ]
    else:  # field_team - identity is the (code, hierarchy_type) pair, never the code alone
        stmt = (
            "SELECT 1 FROM pidilite_demo.gold.access_map_field_team "
            "WHERE lower(user_email) = lower(:email) AND field_team_code = :code "
            "AND hierarchy_type = :ht LIMIT 1"
        )
        params = [
            {"name": "email", "value": email},
            {"name": "code", "value": scope_id},
            {"name": "ht", "value": hierarchy_type},
        ]
    try:
        return len(run_sql(stmt, params)) > 0
    except Exception:
        return False


@st.cache_resource(ttl=1800)
def get_pg_connection():
    cred = w.database.generate_database_credential(instance_names=[INSTANCE_NAME])
    conn = psycopg2.connect(
        host=os.environ["PGHOST"],
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=cred.token,
        sslmode=os.environ.get("PGSSLMODE", "require"),
    )
    # This connection is cached and reused across reruns for up to 30 minutes,
    # not held open for a single multi-statement transaction. Without
    # autocommit, one failed statement (e.g. a permission error) leaves the
    # transaction aborted and every subsequent command on this same cached
    # connection fails with InFailedSqlTransaction, even after the underlying
    # issue is fixed - hit this for real. Autocommit makes each statement its
    # own transaction, so a failure never poisons later ones.
    conn.autocommit = True
    return conn


email = viewer_email()
if not email:
    st.stop()

st.write(f"Commenting as **{email}**")

# Arriving from a dashboard row-link pre-fills everything below via URL params
# (?scope_type=customer&scope_id=94&customer_name=...), so nobody has to
# remember or type a customer code / field team code by hand. Opening the app
# directly (no params) falls back to the manual form.
params = st.query_params
linked = "scope_type" in params and "scope_id" in params

if linked:
    scope_type = params["scope_type"]
    scope_id = params["scope_id"]
    hierarchy_type = params.get("hierarchy_type")
    label = params.get("customer_name") or (
        f"{scope_id} ({hierarchy_type})" if scope_type == "field_team" else scope_id
    )
    st.info(f"Commenting on **{scope_type.replace('_', ' ')}**: {label}")
else:
    scope_type = st.selectbox("What are you commenting on?", ["customer", "field_team"])
    hierarchy_type = None
    if scope_type == "customer":
        scope_id = st.text_input("Customer code")
    else:
        scope_id = st.text_input("Field team code (e.g. WSSTTY1)")
        hierarchy_type = st.selectbox("Hierarchy", ["Sales Hierarchy", "MDI Hierarchy"])

comment_text = st.text_area("Comment")

if st.button("Add comment", type="primary"):
    if not scope_id or not comment_text:
        st.error("Fill in both the record and the comment.")
    elif not is_authorized(email, scope_type, scope_id, hierarchy_type):
        st.error(f"You don't have access to comment on this {scope_type.replace('_', ' ')}.")
    else:
        conn = get_pg_connection()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.comments (scope_type, scope_id, user_email, comment_text) "
                "VALUES (%s, %s, %s, %s)",
                (scope_type, scope_id, email, comment_text),
            )
        st.success("Comment added — it's queryable from the analytical side immediately.")

st.divider()
st.subheader("Recent comments on this record")
if scope_id:
    conn = get_pg_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT user_email, comment_text, created_at FROM public.comments "
            "WHERE scope_type = %s AND scope_id = %s ORDER BY created_at DESC LIMIT 20",
            (scope_type, scope_id),
        )
        rows = cur.fetchall()
    if rows:
        for author, text, created_at in rows:
            st.markdown(f"**{author}** · _{created_at}_  \n{text}")
    else:
        st.caption("No comments yet.")
