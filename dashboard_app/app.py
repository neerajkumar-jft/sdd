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
import os
from urllib.parse import quote

import pandas as pd
import plotly.express as px
import psycopg2
import streamlit as st
import streamlit.components.v1 as components
from databricks import sql
from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config

st.set_page_config(page_title="X Industries — Sales Dashboard", layout="wide")

WAREHOUSE_ID = "be5dd2cb70eb66ee"
LAKEBASE_INSTANCE = "pidilite-comments"
COMMENT_APP_URL = "https://pidilite-comments-app-7474658069346952.aws.databricksapps.com"

# Ambient auth as THIS APP'S OWN service principal - deliberately not the
# viewer's OBO identity. "My Comments" below is scoped by a literal
# `WHERE user_email = <viewer's email>` filter instead, which is what the
# user actually asked for ("who opens the url, the data is shown for them
# only") and is simpler than wiring OBO through to Postgres as well.
w = WorkspaceClient()


def viewer_email() -> str:
    return st.context.headers.get("X-Forwarded-Email", "")


def user_token() -> str:
    return st.context.headers.get("X-Forwarded-Access-Token", "")


@st.cache_resource(ttl=300)
def get_connection(token: str):
    cfg = Config()
    host = cfg.host.replace("https://", "").replace("http://", "")
    try:
        return sql.connect(
            server_hostname=host,
            http_path=f"/sql/1.0/warehouses/{WAREHOUSE_ID}",
            access_token=token,
        )
    except Exception as err:
        # The connector's default traceback only shows "Error during request
        # to server", swallowing the actual HTTP code and server error message
        # it already captured. Surface those so an auth/scope rejection (403)
        # is distinguishable from a real network problem at a glance - this is
        # what caught Shivam's stale-OAuth-grant 403 instead of us guessing.
        detail = getattr(err, "message_with_context", lambda: str(err))()
        st.error(f"SQL warehouse connection failed:\n\n{detail}")
        raise


@st.cache_resource(ttl=1800)
def get_pg_connection():
    cred = w.database.generate_database_credential(instance_names=[LAKEBASE_INSTANCE])
    conn = psycopg2.connect(
        host=os.environ["PGHOST"],
        port=os.environ.get("PGPORT", "5432"),
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=cred.token,
        sslmode=os.environ.get("PGSSLMODE", "require"),
    )
    # Cached and reused across reruns, not held for one transaction - a single
    # failed statement must not poison every later one on this connection.
    conn.autocommit = True
    return conn


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

ROLE_LEVELS = {
    "Territory/Area Sales Manager": 1,
    "Regional/Zonal Sales Manager": 2,
    "National Sales Manager": 3,
    "Head Office": 4,
}


# Streamlit reruns this whole script top-to-bottom on EVERY widget interaction
# (editing a cell in "My Comments", clicking Save, anything) - without caching,
# every one of those reruns re-fires all 7 warehouse queries below, even
# though only the Lakebase comments actually changed. Cached per (token,
# email) - each viewer's own OBO token, so no risk of one viewer's cached rows
# being served to another; a cache hit for the same viewer is the whole point.
@st.cache_data(ttl=300, show_spinner="Loading your data…")
def load_dashboard_data(token: str, email: str):
    conn = get_connection(token)

    # Viewer's own role - dim_person carries no row filter (it's the internal
    # org roster, already exposed to Genie), so this just resolves who's
    # asking rather than restricting anything. Used to size the dashboard to
    # the role: each tier gets one more layer of cross-comparison than the
    # tier below it, since RLS has already cut the rows down to just that
    # scope - a Territory Manager's "revenue by territory" chart would just
    # be a single bar.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT role FROM pidilite_demo.gold.dim_person WHERE lower(user_email) = lower(:email) LIMIT 1",
            {"email": email},
        )
        _role_row = cur.fetchone()
    role = _role_row[0] if _role_row else None

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
    # Grouped by person_id, not person_name: two people in a real roster can
    # share a name, and grouping on the name would silently merge their figures.
    salespeople = run_query(
        conn,
        "SELECT p.person_id, p.person_name, p.role, "
        "       SUM(f.revenue) AS revenue, COUNT(*) AS transactions "
        "FROM pidilite_demo.gold.fact_sales_transaction f "
        "JOIN pidilite_demo.gold.dim_person p ON p.person_id = f.salesperson_id "
        "GROUP BY p.person_id, p.person_name, p.role ORDER BY revenue DESC",
    )
    field_teams = run_query(
        conn,
        "SELECT ft.field_team_code, ft.hierarchy_type, ft.division_id, "
        "ft.master_person_id, ft.ra1_person_id, ft.ra2_person_id, "
        "pm.person_name AS master_name, "
        "p1.person_name AS region_name, p2.person_name AS nation_name "
        "FROM pidilite_demo.gold.dim_field_team ft "
        "LEFT JOIN pidilite_demo.gold.dim_person pm ON pm.person_id = ft.master_person_id "
        "LEFT JOIN pidilite_demo.gold.dim_person p1 ON p1.person_id = ft.ra1_person_id "
        "LEFT JOIN pidilite_demo.gold.dim_person p2 ON p2.person_id = ft.ra2_person_id "
        "ORDER BY ft.field_team_code",
    )
    # "Region" and "nation" aren't separate dimensions - dim_field_team already
    # carries ra1_person_id/ra2_person_id, so a region IS just "the territories
    # reporting to this particular Zonal Manager". Merged onto territory_month
    # once here so every comparison chart below (grouped by territory, region,
    # or nation) reads off the same enriched frame.
    territory_month = territory_month.merge(
        field_teams[[
            "field_team_code", "hierarchy_type",
            "ra1_person_id", "region_name",
            "ra2_person_id", "nation_name",
        ]],
        on=["field_team_code", "hierarchy_type"],
        how="left",
    )

    # Which manager each salesperson reports to, so the performance chart can
    # be coloured by region. A manager can hold more than one territory, so
    # dedupe rather than assuming one row per person.
    salespeople = salespeople.merge(
        field_teams[["master_person_id", "region_name"]]
        .drop_duplicates(subset=["master_person_id"])
        .rename(columns={"master_person_id": "person_id"}),
        on="person_id",
        how="left",
    )

    return role, dealers, territory_month, category, category_by_month, divisions, salespeople, field_teams


viewer_role, dealers, territory_month, category, category_by_month, divisions, salespeople, field_teams = (
    load_dashboard_data(token, email)
)
# Unrecognized role fails open to the fullest view - this only ever adds or
# removes CHART SECTIONS, never row access, so there's nothing unsafe about
# defaulting broad for a login dim_person doesn't recognize.
viewer_level = ROLE_LEVELS.get(viewer_role, 4)

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
# 3. Territory & hierarchy analysis - each tier of management gets one more
# layer of cross-comparison than the tier below it, since RLS has already cut
# the rows down to just what that tier manages:
#   Territory Manager (1): none - exactly one territory, every chart here
#     would be a single bar.
#   Regional Manager (2): the territories under their own region, compared.
#   National Manager (3): + the regions under their own nation, compared,
#     + the divisions they span.
#   Head Office (4): + nations compared, + the Sales vs. MDI hierarchy split.
# ===========================================================================
st.header("Territory & Hierarchy")

if viewer_level == 1:
    st.caption("Hidden at territory level — these are cross-territory comparisons, and you have exactly one.")
else:
    if viewer_level >= 4:
        by_nation = (
            territory_month.groupby(["ra2_person_id", "nation_name"], dropna=False)["revenue"]
            .sum().reset_index().sort_values("revenue", ascending=False)
        )
        if len(by_nation):
            st.plotly_chart(px.bar(by_nation, x="nation_name", y="revenue", title="Revenue by National Manager"), use_container_width=True)

    if viewer_level >= 3:
        by_region = (
            territory_month.groupby(["ra1_person_id", "region_name"], dropna=False)["revenue"]
            .sum().reset_index().sort_values("revenue", ascending=False)
        )
        if len(by_region):
            st.plotly_chart(px.bar(by_region, x="region_name", y="revenue", title="Revenue by Region (Zonal Manager)"), use_container_width=True)

    by_territory = territory_month.groupby("field_team_code")["revenue"].sum().reset_index().sort_values("revenue", ascending=False)
    if len(by_territory):
        st.plotly_chart(px.bar(by_territory, x="field_team_code", y="revenue", title="Revenue by Territory"), use_container_width=True)

    if viewer_level >= 3:
        by_division = territory_month.groupby("division_id")["revenue"].sum().reset_index().merge(divisions, on="division_id", how="left")
        if len(by_division):
            st.plotly_chart(px.bar(by_division, x="division_name", y="revenue", title="Revenue by Division"), use_container_width=True)

    if viewer_level >= 4:
        by_hierarchy = territory_month.groupby("hierarchy_type")["revenue"].sum().reset_index()
        if len(by_hierarchy):
            st.plotly_chart(px.pie(by_hierarchy, names="hierarchy_type", values="revenue", hole=0.5, title="Sales vs. MDI Hierarchy"), use_container_width=True)

    latest_active = (
        territory_month.groupby(["field_team_code", "month"])["active_dealers"].sum().reset_index()
        .sort_values("month").groupby("field_team_code").tail(1)
        .sort_values("active_dealers", ascending=False)
    )
    if len(latest_active):
        st.plotly_chart(px.bar(latest_active, x="field_team_code", y="active_dealers", title="Active Dealers by Territory (latest month)"), use_container_width=True)

    heat = territory_month.groupby(["field_team_code", "month"])["revenue"].sum().reset_index()
    if len(heat):
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

    # Cross-state comparison - same reasoning as "Territory & Hierarchy" above:
    # a Territory/Area Sales Manager's dealers are typically one state, so this
    # would just be a single bar.
    if viewer_level >= 2:
        by_state = dealers.groupby("state")["revenue_total"].sum().reset_index().sort_values("revenue_total", ascending=False)
        pcol_b.plotly_chart(px.bar(by_state, x="state", y="revenue_total", title="Revenue by State"), use_container_width=True)

# ===========================================================================
# 6. Management performance. Meaningless at territory level, where "the team"
# is just yourself.
#
# fact_sales_transaction.salesperson_id is always the Territory/Area Sales
# Manager of the customer's field team, so this chart is always at territory
# grain regardless of who is looking. That is deliberate - it ranks individual
# performers, which is a real thing a National Manager wants. The comparison
# against a viewer's own DIRECT reports lives in "Territory & Hierarchy" above,
# where a National Manager gets their Zonal Managers and Head Office gets their
# National Managers. So the heading here says whose managers these are rather
# than implying they report to the viewer directly.
# ===========================================================================
if viewer_level >= 2:
    st.header("Management Performance")
    if len(salespeople):
        # Colour by region, not by role: every salesperson here holds the same
        # role, so colouring by it produces a one-entry legend that says
        # nothing. Region tells you which Zonal Manager each one sits under,
        # which is the grouping that actually varies.
        heading = (
            "Revenue by Territory Manager — my team"
            if viewer_level == 2
            else "Revenue by Territory Manager, grouped by Zonal Manager"
        )
        colour = "region_name" if salespeople["region_name"].nunique() > 1 else None
        st.plotly_chart(
            px.bar(
                salespeople, x="person_name", y="revenue",
                color=colour, title=heading,
                labels={"person_name": "Territory Manager", "revenue": "Revenue", "region_name": "Zonal Manager"},
            ),
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

# ===========================================================================
# My Comments - every comment THIS viewer has written, across whichever
# customers/territories they've commented on. Scoped by a literal
# `WHERE user_email = <viewer>` filter - deliberately not the record-scoped
# read the comment app uses, since the point here is "my history", not "this
# record's comments". Editable inline (comment_text only); Save runs a
# per-changed-row UPDATE, always re-checking `AND user_email = %s` so this
# can never touch a comment that isn't the viewer's, even if a bug elsewhere
# let a bad comment_id through.
# ===========================================================================
st.header("My Comments")

pg_conn = get_pg_connection()
with pg_conn.cursor() as cur:
    cur.execute(
        "SELECT comment_id, scope_type, scope_id, comment_text, created_at, updated_at "
        "FROM public.comments WHERE user_email = %s ORDER BY created_at DESC",
        (email,),
    )
    my_rows = cur.fetchall()
    my_cols = [d[0] for d in cur.description]
my_comments = pd.DataFrame(my_rows, columns=my_cols)

if len(my_comments):
    def _record_label(row):
        if row["scope_type"] == "customer":
            match = dealers[dealers["customer_code"].astype(str) == str(row["scope_id"])]
            return match["customer_name"].iloc[0] if len(match) else str(row["scope_id"])
        match = field_teams[field_teams["field_team_code"] == row["scope_id"]]
        if len(match):
            return f"{row['scope_id']} ({match['hierarchy_type'].iloc[0]})"
        return str(row["scope_id"])

    my_comments["record"] = my_comments.apply(_record_label, axis=1)
    original = my_comments.set_index("comment_id")[
        ["scope_type", "record", "comment_text", "created_at", "updated_at"]
    ]

    edited = st.data_editor(
        original,
        column_config={
            "scope_type": st.column_config.TextColumn("Type", disabled=True),
            "record": st.column_config.TextColumn("Record", disabled=True),
            "comment_text": st.column_config.TextColumn("Comment", disabled=False),
            "created_at": st.column_config.DatetimeColumn("Created", disabled=True),
            "updated_at": st.column_config.DatetimeColumn("Last edited", disabled=True),
        },
        hide_index=True,
        use_container_width=True,
        key="my_comments_editor",
    )

    if st.button("Save changes to my comments"):
        changed = 0
        for comment_id in original.index:
            old_text = original.loc[comment_id, "comment_text"]
            new_text = edited.loc[comment_id, "comment_text"]
            if new_text != old_text:
                with pg_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE public.comments SET comment_text = %s, updated_at = now() "
                        "WHERE comment_id = %s AND user_email = %s",
                        (new_text, comment_id, email),
                    )
                changed += 1
        if changed:
            st.success(f"Updated {changed} comment(s).")
            st.rerun()
        else:
            st.info("No changes to save.")
else:
    st.caption("You haven't added any comments yet.")

# ===========================================================================
# Floating Genie chat bubble, bottom-right.
#
# Moving this to be the LAST st.markdown call in the script was not enough -
# still got painted over by a chart (verified via screenshot). Streamlit
# nests every block, including the last one, inside its own shared wrapping
# containers, so "last in the script" isn't "last child of <body>". A Plotly
# chart is itself rendered inside an iframe, and iframe-vs-regular-content
# stacking is exactly where z-index gets unreliable across browsers.
#
# Fix: st.components.v1.html runs in its OWN throwaway iframe (height=0, never
# visible), but its <script> can reach through same-origin window.parent and
# inject the real widget as a direct child of the actual page's <body> -
# genuinely last in the DOM, outside every one of Streamlit's own containers,
# not just last among them. The `if (parent.document.getElementById(...))
# return` guard stops it from re-inserting a duplicate on every rerun (this
# script re-executes each time, since Streamlit reruns top to bottom).
#
# Auth is whatever the viewer's own browser already has open with Databricks -
# same SSO session, same per-user row filters as every other Genie answer
# verified earlier in this project; embedding changes nothing about that.
# ===========================================================================
GENIE_EMBED_URL = (
    "https://dbc-b53b2bf8-6950.cloud.databricks.com/embed/genie/rooms/"
    "01f1aaa58a6c1e639422daac2f1a1dd9?o=7474658069346952"
)
components.html(
    f"""
    <script>
    (function() {{
        // window.top, not window.parent: Databricks Apps appears to wrap the
        // Streamlit app in its own outer frame, so .parent only escapes ONE
        // level and still lands inside a constrained ancestor - which is
        // exactly why max z-index wasn't enough. .top always reaches the
        // real, outermost window regardless of how many frames are nested.
        const doc = window.top.document;
        // Always tear down and rebuild rather than "if present, skip":
        // Streamlit reruns this script on every interaction, but a stale
        // element from an OLDER deploy - injected earlier in this same
        // long-lived browser tab, before a CSS/z-index fix shipped - would
        // otherwise sit there forever, since a plain existence check has no
        // way to tell "already correct" from "already stale."
        const oldWidget = doc.getElementById('genie-widget');
        if (oldWidget) {{ oldWidget.remove(); }}
        const oldStyle = doc.getElementById('genie-widget-style');
        if (oldStyle) {{ oldStyle.remove(); }}

        const style = doc.createElement('style');
        style.id = 'genie-widget-style';
        style.textContent = `
            #genie-widget {{
                position: fixed; bottom: 24px; right: 24px; z-index: 2147483647;
                display: flex; flex-direction: column-reverse; align-items: flex-end; gap: 12px;
            }}
            #genie-widget summary {{
                list-style: none; width: 56px; height: 56px; border-radius: 50%;
                background: #FF3621; color: white; display: flex; align-items: center;
                justify-content: center; font-size: 26px; cursor: pointer;
                box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            }}
            #genie-widget summary::-webkit-details-marker {{ display: none; }}
            #genie-widget iframe {{
                position: relative; z-index: 2147483647;
                width: min(480px, calc(100vw - 48px));
                height: min(720px, calc(100vh - 120px));
                border: none; border-radius: 12px;
                box-shadow: 0 8px 30px rgba(0,0,0,0.35);
            }}
            #genie-widget summary {{ position: relative; z-index: 2147483647; }}
        `;
        doc.head.appendChild(style);

        const details = doc.createElement('details');
        details.id = 'genie-widget';
        details.innerHTML =
            '<summary>💬</summary>' +
            '<iframe src="{GENIE_EMBED_URL}" allow="clipboard-write"></iframe>';
        doc.body.appendChild(details);
    }})();
    </script>
    """,
    height=0,
)
