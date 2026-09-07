"""
X Industries — Scoped Sales Dashboard (custom, per-viewer RLS-aware).

Runs every query as the VIEWING USER's own identity - Databricks Apps
"on-behalf-of-user" authorization (user_api_scopes: ["sql"]) - so Unity
Catalog's existing row filters (can_see_customer / can_see_field_team) apply
automatically. Same architecture as the AI/BI dashboard (embed_credentials
off there, the user's own token here); this surface just also gets a native
per-row "Comment" link, which Lakeview's table linkUrlTemplate proved
unreliable for.

Both dealers AND territories carry a comment link, opening the companion
comment app (pidilite-comments-app) in a new tab, pre-filled with the record.
"""
import html
from urllib.parse import quote

import pandas as pd
import plotly.express as px
import streamlit as st
from databricks import sql
from databricks.sdk.core import Config

st.set_page_config(page_title="X Industries — Sales Dashboard", layout="wide")

WAREHOUSE_ID = "be5dd2cb70eb66ee"
COMMENT_APP_URL = "https://pidilite-comments-app-7474658069346952.aws.databricksapps.com"


def viewer_email() -> str:
    return st.context.headers.get("X-Forwarded-Email", "")


def user_token() -> str:
    return st.context.headers.get("X-Forwarded-Access-Token", "")


@st.cache_resource(ttl=300)
def get_connection(token: str):
    cfg = Config()
    host = cfg.host.replace("https://", "").replace("http://", "")
    return sql.connect(
        server_hostname=host,
        http_path=f"/sql/1.0/warehouses/{WAREHOUSE_ID}",
        access_token=token,
    )


def run_query(conn, query: str) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(query)
        return cur.fetchall_arrow().to_pandas()


def render_linked_table(df: pd.DataFrame, display_cols: list, link_fn) -> None:
    """Render df as an HTML table with a trailing clickable 'Comment' column."""
    headers = "".join(f"<th style='text-align:left;padding:6px 10px'>{html.escape(c)}</th>" for c in display_cols)
    headers += "<th style='text-align:left;padding:6px 10px'>Comment</th>"
    rows_html = []
    for _, r in df.iterrows():
        cells = "".join(f"<td style='padding:6px 10px'>{html.escape(str(r[c]))}</td>" for c in display_cols)
        cells += f"<td style='padding:6px 10px'><a href='{link_fn(r)}' target='_blank' rel='noopener'>💬 Comment</a></td>"
        rows_html.append(f"<tr>{cells}</tr>")
    table = (
        "<table style='width:100%;border-collapse:collapse;font-size:14px'>"
        f"<thead><tr>{headers}</tr></thead><tbody>{''.join(rows_html)}</tbody></table>"
    )
    st.markdown(table, unsafe_allow_html=True)


email = viewer_email()
token = user_token()

if not email or not token:
    st.error(
        "Couldn't identify you. This app must be opened through Databricks Apps "
        "with user authorization enabled, not visited directly or with an "
        "expired session."
    )
    st.stop()

st.title("X Industries — Sales Dashboard")
st.caption(f"Viewing as **{email}** — every figure below is scoped to what you're entitled to see.")

conn = get_connection(token)

# --- KPIs --------------------------------------------------------------------
kpi = run_query(
    conn,
    "SELECT SUM(revenue_total) AS revenue, SUM(transactions) AS txns, "
    "COUNT_IF(NOT is_dormant) AS active, COUNT_IF(is_dormant) AS dormant "
    "FROM pidilite_demo.gold.agg_dealer_scorecard",
)
row = kpi.iloc[0] if len(kpi) else None
c1, c2, c3, c4 = st.columns(4)
c1.metric("Total Revenue (Rs)", f"{(row['revenue'] or 0):,.0f}" if row is not None else "0")
c2.metric("Total Transactions", f"{(row['txns'] or 0):,.0f}" if row is not None else "0")
c3.metric("Active Dealers", int(row["active"] or 0) if row is not None else 0)
c4.metric("Dormant Dealers", int(row["dormant"] or 0) if row is not None else 0)

# --- Charts --------------------------------------------------------------------
trend = run_query(
    conn,
    "SELECT month, SUM(revenue) AS revenue FROM pidilite_demo.gold.agg_sales_by_territory_month "
    "GROUP BY month ORDER BY month",
)
if len(trend):
    st.plotly_chart(px.line(trend, x="month", y="revenue", title="Revenue Trend by Month"), use_container_width=True)

col_a, col_b = st.columns(2)
by_territory = run_query(
    conn,
    "SELECT field_team_code, SUM(revenue) AS revenue FROM pidilite_demo.gold.agg_sales_by_territory_month "
    "GROUP BY field_team_code ORDER BY revenue DESC",
)
if len(by_territory):
    col_a.plotly_chart(px.bar(by_territory, x="field_team_code", y="revenue", title="Revenue by Territory"), use_container_width=True)

by_category = run_query(
    conn,
    "SELECT product_category, SUM(revenue) AS revenue FROM pidilite_demo.gold.fact_sales_transaction "
    "GROUP BY product_category ORDER BY revenue DESC",
)
if len(by_category):
    col_b.plotly_chart(px.bar(by_category, x="product_category", y="revenue", title="Revenue by Product Category"), use_container_width=True)

# --- Dealers: comment link per customer --------------------------------------
st.subheader("Dealers")
customers = run_query(
    conn,
    "SELECT customer_code, customer_name, city, state, revenue_total, revenue_recent, "
    "days_since_last_sale, is_dormant, top_category "
    "FROM pidilite_demo.gold.agg_dealer_scorecard ORDER BY revenue_total DESC",
)
if len(customers):
    render_linked_table(
        customers,
        display_cols=["customer_name", "city", "state", "revenue_total", "revenue_recent",
                      "days_since_last_sale", "is_dormant", "top_category"],
        link_fn=lambda r: (
            f"{COMMENT_APP_URL}/?scope_type=customer&scope_id={quote(str(r['customer_code']))}"
            f"&customer_name={quote(str(r['customer_name']))}"
        ),
    )
else:
    st.caption("No dealers visible to your role.")

# --- Territories: comment link per field team --------------------------------
st.subheader("Territories")
field_teams = run_query(
    conn,
    "SELECT ft.field_team_code, ft.hierarchy_type, ft.division_id, p.person_name AS master_name "
    "FROM pidilite_demo.gold.dim_field_team ft "
    "LEFT JOIN pidilite_demo.gold.dim_person p ON p.person_id = ft.master_person_id "
    "ORDER BY ft.field_team_code",
)
if len(field_teams):
    render_linked_table(
        field_teams,
        display_cols=["field_team_code", "hierarchy_type", "division_id", "master_name"],
        link_fn=lambda r: (
            f"{COMMENT_APP_URL}/?scope_type=field_team&scope_id={quote(str(r['field_team_code']))}"
            f"&hierarchy_type={quote(str(r['hierarchy_type']))}"
        ),
    )
else:
    st.caption("No territories visible to your role.")
