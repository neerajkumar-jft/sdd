"""
X Industries — Scoped Sales Dashboard (custom, per-viewer RLS-aware).

Runs every query as the VIEWING USER's own identity - Databricks Apps
"on-behalf-of-user" authorization (user_api_scopes: ["sql"]) - so Unity
Catalog's existing row filters (can_see_customer / can_see_field_team) apply
automatically. Same architecture as the AI/BI dashboard (embed_credentials
off there, the user's own token here); this surface just also gets a native
per-row "Comment" link, which Lakeview's table linkUrlTemplate proved
unreliable for.

Query count is kept deliberately small: most charts are DERIVED in pandas
from a handful of richer pulls (the full agg_dealer_scorecard grain, the
full agg_sales_by_territory_month grain) rather than one round-trip per
chart - fewer queries against a viewer-scoped connection that's slower to
warm up than the app's own service-principal queries would be.
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

# ---------------------------------------------------------------------------
# Data pulls - a handful of richer queries; almost everything below is
# derived from these three dataframes in pandas rather than re-queried.
# ---------------------------------------------------------------------------
dealers = run_query(
    conn,
    "SELECT customer_code, customer_name, field_team_code, hierarchy_type, division_id, city, state, "
    "revenue_total, revenue_recent, transactions, first_sale, last_sale, days_since_last_sale, "
    "is_dormant, top_category, as_of_date "
    "FROM pidilite_demo.gold.agg_dealer_scorecard",
)
territory_month = run_query(
    conn,
    "SELECT field_team_code, hierarchy_type, division_id, month, revenue, quantity, transactions, active_dealers "
    "FROM pidilite_demo.gold.agg_sales_by_territory_month",
)
category = run_query(
    conn,
    "SELECT product_category, SUM(quantity) AS quantity, SUM(revenue) AS revenue "
    "FROM pidilite_demo.gold.fact_sales_transaction GROUP BY product_category ORDER BY revenue DESC",
)
category_by_month = run_query(
    conn,
    "SELECT date_trunc('month', transaction_date) AS month, product_category, SUM(revenue) AS revenue "
    "FROM pidilite_demo.gold.fact_sales_transaction GROUP BY 1, 2 ORDER BY 1",
)
divisions = run_query(conn, "SELECT division_id, division_name FROM pidilite_demo.gold.dim_division")
salespeople = run_query(
    conn,
    "SELECT p.person_name, p.role, SUM(f.revenue) AS revenue, COUNT(*) AS transactions "
    "FROM pidilite_demo.gold.fact_sales_transaction f "
    "JOIN pidilite_demo.gold.dim_person p ON p.person_id = f.salesperson_id "
    "GROUP BY p.person_name, p.role ORDER BY revenue DESC",
)
field_teams = run_query(
    conn,
    "SELECT ft.field_team_code, ft.hierarchy_type, ft.division_id, p.person_name AS master_name "
    "FROM pidilite_demo.gold.dim_field_team ft "
    "LEFT JOIN pidilite_demo.gold.dim_person p ON p.person_id = ft.master_person_id "
    "ORDER BY ft.field_team_code",
)

no_data = len(dealers) == 0
if no_data:
    st.warning("No dealers visible to your role — the sections below will be empty.")

as_of_date = dealers["as_of_date"].max() if len(dealers) else None

# ===========================================================================
# 1. Executive summary
# ===========================================================================
st.header("Executive Summary")

total_revenue = dealers["revenue_total"].sum() if len(dealers) else 0
total_txns = dealers["transactions"].sum() if len(dealers) else 0
active_count = int((~dealers["is_dormant"]).sum()) if len(dealers) else 0
dormant_count = int(dealers["is_dormant"].sum()) if len(dealers) else 0
avg_order_value = (total_revenue / total_txns) if total_txns else 0
new_dealers = 0
if len(dealers) and as_of_date is not None:
    cutoff = pd.Timestamp(as_of_date) - pd.Timedelta(days=90)
    new_dealers = int((pd.to_datetime(dealers["first_sale"]) >= cutoff).sum())

month_rev = (
    territory_month.groupby("month")["revenue"].sum().reset_index().sort_values("month")
    if len(territory_month) else pd.DataFrame(columns=["month", "revenue"])
)
mom_growth = None
if len(month_rev) >= 2:
    last, prev = month_rev["revenue"].iloc[-1], month_rev["revenue"].iloc[-2]
    if prev:
        mom_growth = (last - prev) / prev * 100

c1, c2, c3, c4 = st.columns(4)
c1.metric("Total Revenue (Rs)", f"{total_revenue:,.0f}")
c2.metric("Total Transactions", f"{total_txns:,.0f}")
c3.metric("Active Dealers", active_count)
c4.metric("Dormant Dealers", dormant_count)

c5, c6, c7 = st.columns(3)
c5.metric("Avg Order Value (Rs)", f"{avg_order_value:,.0f}")
c6.metric("New Dealers (last 90d)", new_dealers)
c7.metric("Revenue vs Prior Month", f"{mom_growth:+.1f}%" if mom_growth is not None else "—")

# ===========================================================================
# 2. Trends & seasonality
# ===========================================================================
st.header("Trends & Seasonality")

month_txn = (
    territory_month.groupby("month")["transactions"].sum().reset_index().sort_values("month")
    if len(territory_month) else pd.DataFrame(columns=["month", "transactions"])
)

tcol1, tcol2 = st.columns(2)
if len(month_rev):
    tcol1.plotly_chart(px.line(month_rev, x="month", y="revenue", title="Revenue Trend by Month"), use_container_width=True)
if len(month_txn):
    tcol2.plotly_chart(px.line(month_txn, x="month", y="transactions", title="Transaction Volume by Month"), use_container_width=True)

if len(month_rev) >= 2:
    growth_df = month_rev.copy()
    growth_df["growth_pct"] = growth_df["revenue"].pct_change() * 100
    growth_df = growth_df.dropna()
    st.plotly_chart(
        px.bar(growth_df, x="month", y="growth_pct", title="Month-over-Month Revenue Growth (%)"),
        use_container_width=True,
    )

if len(category_by_month):
    st.plotly_chart(
        px.area(category_by_month, x="month", y="revenue", color="product_category", title="Revenue by Month, by Product Category"),
        use_container_width=True,
    )

# ===========================================================================
# 3. Territory & hierarchy analysis
# ===========================================================================
st.header("Territory & Hierarchy")

by_territory = (
    territory_month.groupby("field_team_code")["revenue"].sum().reset_index().sort_values("revenue", ascending=False)
    if len(territory_month) else pd.DataFrame(columns=["field_team_code", "revenue"])
)
by_hierarchy = (
    territory_month.groupby("hierarchy_type")["revenue"].sum().reset_index()
    if len(territory_month) else pd.DataFrame(columns=["hierarchy_type", "revenue"])
)
by_division = (
    territory_month.groupby("division_id")["revenue"].sum().reset_index().merge(divisions, on="division_id", how="left")
    if len(territory_month) else pd.DataFrame(columns=["division_id", "revenue", "division_name"])
)

hcol1, hcol2 = st.columns(2)
if len(by_territory):
    hcol1.plotly_chart(px.bar(by_territory, x="field_team_code", y="revenue", title="Revenue by Territory"), use_container_width=True)
if len(by_hierarchy):
    hcol2.plotly_chart(px.pie(by_hierarchy, names="hierarchy_type", values="revenue", hole=0.5, title="Sales vs. MDI Hierarchy"), use_container_width=True)

dcol1, dcol2 = st.columns(2)
if len(by_division):
    dcol1.plotly_chart(px.bar(by_division, x="division_name", y="revenue", title="Revenue by Division"), use_container_width=True)

if len(territory_month):
    latest_active = (
        territory_month.groupby(["field_team_code", "month"])["active_dealers"].sum().reset_index()
        .sort_values("month").groupby("field_team_code").tail(1)
        .sort_values("active_dealers", ascending=False)
    )
    dcol2.plotly_chart(px.bar(latest_active, x="field_team_code", y="active_dealers", title="Active Dealers by Territory (latest month)"), use_container_width=True)

if len(territory_month):
    heat = territory_month.groupby(["field_team_code", "month"])["revenue"].sum().reset_index()
    pivot = heat.pivot(index="field_team_code", columns="month", values="revenue").fillna(0)
    st.plotly_chart(px.imshow(pivot, aspect="auto", title="Territory × Month Revenue Heatmap", labels=dict(color="Revenue")), use_container_width=True)

# ===========================================================================
# 4. Product mix
# ===========================================================================
st.header("Product Mix")

pcol1, pcol2, pcol3 = st.columns(3)
if len(category):
    pcol1.plotly_chart(px.bar(category, x="product_category", y="revenue", title="Revenue by Product Category"), use_container_width=True)
    pcol2.plotly_chart(px.pie(category, names="product_category", values="revenue", hole=0.5, title="Category Share of Revenue"), use_container_width=True)
    pcol3.plotly_chart(px.bar(category, x="product_category", y="quantity", title="Quantity Sold by Category"), use_container_width=True)

# ===========================================================================
# 5. Dealer health
# ===========================================================================
st.header("Dealer Health")

hc1, hc2 = st.columns(2)
if len(dealers):
    health = pd.DataFrame({"status": ["Active", "Dormant"], "count": [active_count, dormant_count]})
    hc1.plotly_chart(px.pie(health, names="status", values="count", hole=0.5, title="Dealer Health"), use_container_width=True)
    hc2.plotly_chart(px.histogram(dealers, x="days_since_last_sale", title="Days Since Last Sale (distribution)", nbins=30), use_container_width=True)

pcol_a, pcol_b = st.columns(2)
if len(dealers):
    pareto = dealers.sort_values("revenue_total", ascending=False).reset_index(drop=True)
    pareto["rank"] = pareto.index + 1
    pareto["cumulative_pct"] = pareto["revenue_total"].cumsum() / pareto["revenue_total"].sum() * 100
    pcol_a.plotly_chart(px.line(pareto, x="rank", y="cumulative_pct", title="Revenue Concentration (Pareto) — Dealers Ranked by Revenue"), use_container_width=True)

    by_state = dealers.groupby("state")["revenue_total"].sum().reset_index().sort_values("revenue_total", ascending=False)
    pcol_b.plotly_chart(px.bar(by_state, x="state", y="revenue_total", title="Revenue by State"), use_container_width=True)

# ===========================================================================
# 6. Management performance
# ===========================================================================
st.header("Management Performance")
if len(salespeople):
    st.plotly_chart(
        px.bar(salespeople, x="person_name", y="revenue", color="role", title="Revenue by Salesperson"),
        use_container_width=True,
    )

# ===========================================================================
# Dealers: comment link per customer
# ===========================================================================
st.header("Dealers")
if len(dealers):
    render_linked_table(
        dealers.sort_values("revenue_total", ascending=False),
        display_cols=["customer_name", "city", "state", "revenue_total", "revenue_recent",
                      "days_since_last_sale", "is_dormant", "top_category"],
        link_fn=lambda r: (
            f"{COMMENT_APP_URL}/?scope_type=customer&scope_id={quote(str(r['customer_code']))}"
            f"&customer_name={quote(str(r['customer_name']))}"
        ),
    )
else:
    st.caption("No dealers visible to your role.")

# ===========================================================================
# Territories: comment link per field team, now with performance figures
# ===========================================================================
st.header("Territories")
if len(field_teams):
    territory_totals = (
        territory_month.groupby(["field_team_code", "hierarchy_type"])["revenue"].sum().reset_index()
        if len(territory_month) else pd.DataFrame(columns=["field_team_code", "hierarchy_type", "revenue"])
    )
    field_teams_enriched = field_teams.merge(territory_totals, on=["field_team_code", "hierarchy_type"], how="left")
    field_teams_enriched["revenue"] = field_teams_enriched["revenue"].fillna(0)
    field_teams_enriched = field_teams_enriched.sort_values("revenue", ascending=False)
    render_linked_table(
        field_teams_enriched,
        display_cols=["field_team_code", "hierarchy_type", "division_id", "master_name", "revenue"],
        link_fn=lambda r: (
            f"{COMMENT_APP_URL}/?scope_type=field_team&scope_id={quote(str(r['field_team_code']))}"
            f"&hierarchy_type={quote(str(r['hierarchy_type']))}"
        ),
    )
else:
    st.caption("No territories visible to your role.")
