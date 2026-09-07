# Genie question bank

Questions to run live, with the **correct answer computed from the source CSVs**
so a wrong Genie answer is caught in the room rather than believed.

Every figure below is ground truth for the current seed (anchor date
**2026-08-31**). Regenerating the data changes them all — recompute before a
demo if the seed has moved.

## Dataset shape

| | Rows |
|---|---|
| Dealers | **501** |
| Sales transactions | **26,611** |
| Territories | **17** (12 Sales chain + 5 MDI chain) |
| People | **30** |
| Total revenue, 18 months | **₹70.63 Cr** |
| Date range | 2025-03-01 → 2026-08-31 |

## The four personas

All four are real workspace logins, mapped onto **one reporting chain** in
`data_generation/generate_dims.py`, so scope nests cleanly.

| Persona | Login | Role | Territories | Dealers | Transactions | Revenue | Dormant |
|---|---|---|---|---|---|---|---|
| Abhinav Sarkar | `abhinav.sarkar@` | Territory/Area Sales Manager | 1 | **70** | 6,402 | **₹10.52 Cr** | 11 |
| Akshay Siraswar | `akshay.siraswar@` | Regional/Zonal Sales Manager | 3 | **175** | 13,656 | **₹21.88 Cr** | 34 |
| Shivam Pandey | `shivam.pandey@` | National Sales Manager | 12 | **384** | 21,015 | **₹55.18 Cr** | 78 |
| Neeraj Kumar | `neeraj.kumar@` | Head Office | 17 | **501** | 26,611 | **₹70.63 Cr** | 96 |

Containment holds: **70 ⊂ 175 ⊂ 384 ⊂ 501**.

**Who actually manages what** — of the four real logins, only Abhinav owns a
*territory*. Akshay covers all three of division 10's Sales territories as
Zonal Manager, so the other two keep their generated Territory Managers.

| Territory | Territory Manager | Zonal | National |
|---|---|---|---|
| WSSTTY1 | **Abhinav Sarkar** | Akshay | Shivam |
| WSSTTY2 | Viraj Tiwari *(generated)* | Akshay | Shivam |
| WSSTTY3 | Rushil Saini *(generated)* | Akshay | Shivam |

| Persona | Chains visible |
|---|---|
| Abhinav, Akshay, Shivam | Sales only |
| Neeraj | **Sales *and* MDI** |

---

## ⭐ The two questions worth building the demo around

### 1. "Who manages territory WSSTTY3?"

One question that proves row-level security **and** the dual-hierarchy
modelling at the same time.

| Asked by | Expected answer |
|---|---|
| **Abhinav** | **Nothing visible** — he is on WSSTTY1. Genie should say there is no data visible to him, *not* that the territory does not exist |
| **Akshay** | **1 row** — WSSTTY3 / Sales Hierarchy: **Rushil Saini** → Akshay Siraswar → Shivam Pandey (64 dealers). The MDI row for the same code is filtered out |
| **Shivam** | **1 row** — the same Sales Hierarchy row |
| **Neeraj** | **2 rows** — Sales Hierarchy (Rushil Saini → Akshay → Shivam, 64 dealers) *and* MDI Hierarchy (**Nathaniel Sami → Saumya Mall → Udant Dewan**, 46 dealers) |

Same territory code, two entirely separate management chains and two separate
dealer sets — and only Head Office can see both.

### 2. "Compare the Sales Hierarchy and the MDI Hierarchy by revenue"

| Asked by | Expected answer |
|---|---|
| Abhinav | Sales Hierarchy only — ₹10.52 Cr, 70 dealers |
| Akshay | Sales Hierarchy only — ₹21.88 Cr, 175 dealers |
| Shivam | Sales Hierarchy only — ₹55.18 Cr, 384 dealers |
| **Neeraj** | **Both** — Sales **₹55.18 Cr / 382 dealers**, MDI **₹15.45 Cr / 117 dealers** |

A ₹15.45 Cr business line is **invisible** to three of the four personas. Not
hidden by a dashboard filter — it does not exist in their query results.

---

## Behaviour verified against the live space

Asked through the deployed space as **Akshay** on the *previous, smaller* seed.
The numbers have since changed with the larger seed, but the **behaviour** is
what was being checked, and all of it held:

- **Date anchoring.** Genie volunteered *"The dataset's most recent data is
  through August 31, 2026, so I'm reporting August 2026 as the latest complete
  month."* Today is September — without that instruction, `current_date()`
  would have returned an empty September.
- **Composite territory key.** "Who manages WSSTTY3" returned the Sales
  Hierarchy row only; the MDI row for the same code was filtered out.
- **No self-filtering.** It added no `WHERE user_email = ...` of its own.
- **Scope-aware wording.** It closed with *"these figures reflect your visible
  scope"* rather than presenting the numbers as company totals.
- **Right table for the question.** It chose `agg_dealer_scorecard` over the
  fact table for a dealer-ranking question.

Every figure it reported matched the CSVs exactly on that seed. Re-verify
against the tables below now that the seed has grown.

---

## Per-persona question sets

### Abhinav Sarkar — Territory/Area Sales Manager

| # | Question | Expected |
|---|---|---|
| 1 | What is my total revenue and how many dealers do I cover? | **₹10.52 Cr**, **70 dealers**, 6,402 transactions |
| 2 | Who are my top 5 dealers by revenue? | #150 Mangal Traders ₹97.39 L, #448 Bala Traders ₹96.93 L, #99 Deo Enterprises ₹96.72 L, #231 Basu Building Materials ₹96.57 L, #21 Krishnamurthy Enterprises ₹95.67 L |
| 3 | How many dormant dealers do I have? | **11** |
| 4 | Which product category sells the most for me? | **Sealants**, ₹5.65 Cr |
| 5 | Break my revenue down by division | **Consumer & Bazaar only**, ₹10.52 Cr |
| 6 | Which month was my best? | **October 2025**, ₹0.73 Cr |
| 7 | How many territories do I cover? | **1** — WSSTTY1 |
| 8 | What were sales in August 2026? | ₹49.66 L, 13,696 units, 311 transactions, 50 active dealers |
| 9 | Who manages territory WSSTTY3? | Nothing visible to him |

### Akshay Siraswar — Regional/Zonal Sales Manager

| # | Question | Expected |
|---|---|---|
| 1 | What is my total revenue and how many dealers do I cover? | **₹21.88 Cr**, **175 dealers**, 13,656 transactions |
| 2 | Who are my top 5 dealers? | #77 Dara Hardware Store ₹101.37 L, #246 Sani Agencies ₹98.05 L, #150 Mangal Traders ₹97.39 L, #108 Pandya Building Materials ₹97.23 L, #448 Bala Traders ₹96.93 L |
| 3 | How many dormant dealers do I have? | **34** |
| 4 | How is each of my territories trending over the last 12 months? | 3 territories — WSSTTY1, WSSTTY2, WSSTTY3, all Sales Hierarchy |
| 5 | Which product category sells the most for me? | **Sealants**, ₹11.73 Cr |
| 6 | Which month was my best? | **October 2025**, ₹1.49 Cr |
| 7 | Break my revenue down by division | **Consumer & Bazaar only**, ₹21.88 Cr |
| 8 | What were sales in August 2026? | ₹97.56 L, 27,021 units, 640 transactions, 113 active dealers |
| 9 | Who manages territory WSSTTY3? | 1 row, Sales Hierarchy — Rushil Saini → Akshay → Shivam |

**Q1 against Abhinav's Q1 is the demo moment**: identical question, ₹10.52 Cr
becomes ₹21.88 Cr, 70 dealers becomes 175 — and Abhinav's dealers are a strict
subset of Akshay's.

### Shivam Pandey — National Sales Manager

| # | Question | Expected |
|---|---|---|
| 1 | What is my total revenue and how many dealers do I cover? | **₹55.18 Cr**, **384 dealers**, 21,015 transactions |
| 2 | Who are my top 5 dealers? | #394 Kota Building Materials ₹134.57 L, #293 Bala Paints & Hardware ₹133.30 L, #426 Rai Hardware Store ₹130.30 L, #262 Sachar Agencies ₹129.67 L, #109 Sampath Building Materials ₹128.87 L |
| 3 | Break my revenue down by division | Consumer & Bazaar **₹21.88 Cr**, Waterproofing Solutions **₹13.30 Cr**, Construction Chemicals **₹10.96 Cr**, Industrial Resins **₹9.05 Cr** |
| 4 | How many dormant dealers do I have? | **78** |
| 5 | Which product category sells the most? | **Sealants**, ₹18.87 Cr |
| 6 | Which month was my best? | **May 2026**, ₹3.65 Cr |
| 7 | How many territories do I cover? | **12**, all Sales Hierarchy |
| 8 | What were sales in August 2026? | ₹227.14 L, 39,125 units, 927 transactions, 210 active dealers |
| 9 | Compare Sales and MDI Hierarchy | **Sales only** — MDI is not visible to him |

### Neeraj Kumar — Head Office

| # | Question | Expected |
|---|---|---|
| 1 | What is total revenue and how many dealers are there? | **₹70.63 Cr**, **501 dealers**, 26,611 transactions |
| 2 | Who are the top 5 dealers? | Same five as Shivam's Q2 — the largest accounts all sit in the Sales chain |
| 3 | Break revenue down by division | Consumer & Bazaar **₹27.69 Cr** (221 dealers), Waterproofing Solutions **₹16.84 Cr** (102), Industrial Resins **₹14.26 Cr** (94), Construction Chemicals **₹11.84 Cr** (84) |
| 4 | Compare Sales and MDI Hierarchy | Sales **₹55.18 Cr / 382 dealers**, MDI **₹15.45 Cr / 117 dealers** |
| 5 | How many dormant dealers are there? | **96** (2 of which have never bought at all) |
| 6 | Which product category sells the most? | **Sealants**, ₹23.42 Cr |
| 7 | Which month was the best? | **May 2026**, ₹4.65 Cr |
| 8 | What were sales in August 2026? | ₹295.80 L, 50,177 units, 1,177 transactions, 273 active dealers |
| 9 | Who manages territory WSSTTY3? | **2 rows** — Sales: Rushil Saini → Akshay → Shivam; MDI: Nathaniel Sami → Saumya Mall → Udant Dewan |
| 10 | Which is our biggest division? | **Consumer & Bazaar**, ₹27.69 Cr — as it should be for this business |

Q3 is worth pausing on. Consumer & Bazaar is now the largest business line by
both dealer count and revenue, which is what the real organization looks like.
An earlier seed had it *smallest*, because dealers were spread evenly across
divisions while consumer products are priced far lower — so the division came
out looking a tenth of its real size. Dealer counts are now weighted by
division, which is the actual reason a consumer network is larger.

---

## 🔴 Known weak spots — check these before the client sees them

Genie is a language model over SQL. These are the places it is most likely to
get this dataset wrong, so test them deliberately rather than discovering them
live.

| Risk | What goes wrong | Mitigation |
|---|---|---|
| **Relative dates** | Anchors on `current_date()` → returns nothing, because the data ends 2026-08-31 | Instructions say to anchor on `max(transaction_date)`. **Verified working** — but re-test "last quarter" and "last 3 months" |
| **Fiscal year** | Treats "this year" as Jan–Dec instead of Apr–Mar | Instructions cover it. Test "how did we do this year" and check which window it used |
| **Summing the two chains** | Adds Sales + MDI into a single total without labelling | Instructions forbid it. Test "what is total revenue by hierarchy" |
| **Adding a user predicate** | Writes `WHERE user_email = ...` and double-filters to zero rows | Instructions forbid it explicitly. **Verified working** |
| **Territory code alone** | Joins on `field_team_code` without `hierarchy_type`, merging two territories | Test "who manages WSSTTY3" as Neeraj — a single row instead of two means it got this wrong |
| **Invented metrics** | Answers a target/quota/margin question by substituting revenue | Instructions forbid it. Test "did I hit my target this quarter" — the right answer is "that data is not available" |

---

## Running the benchmark eval

The space ships with 12 benchmark questions (question + expected SQL) in
`genie/pidilite_demo.geniespace.json`, so accuracy can be measured rather than
eyeballed:

```bash
databricks genie genie-create-eval-run   --profile pidilite   # kick off a run
databricks genie genie-list-eval-runs    --profile pidilite   # find the run
databricks genie genie-list-eval-results --profile pidilite   # per-question results
```

Ad-hoc single question against the space:

```bash
databricks genie start-conversation 01f1aaa58a6c1e639422daac2f1a1dd9 \
  "Who are my top 5 dealers by revenue?" --profile pidilite
```

⚠️ The eval and `start-conversation` run **as whoever the profile
authenticates as**. The `pidilite` profile is Shadab, who is *not* in the
access map — so he sees zero rows and every answer looks empty. To test a
persona, sign in as them in the Genie UI:

```
https://dbc-b53b2bf8-6950.cloud.databricks.com/genie/rooms/01f1aaa58a6c1e639422daac2f1a1dd9?w=7474658069346952
```

Each persona also needs `CAN_USE` on the SQL warehouse, not just `CAN_RUN` on
the space — without it the space opens but every message fails with
*"not authorized to use or monitor this SQL Endpoint"*.
