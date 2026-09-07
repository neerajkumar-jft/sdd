# Genie question bank

Questions to run live, with the **correct answer computed from the source CSVs**
so a wrong Genie answer is caught in the room rather than believed.

Every figure below is ground truth as of the current seed (anchor date
**2026-08-31**). Regenerating the data changes them — recompute before a demo if
the seed has moved.

## The four personas

All four are real workspace logins, mapped onto **one reporting chain** in
`data_generation/generate_dims.py`, so scope nests cleanly.

| Persona | Login | Role | Territories | Dealers | Transactions | Revenue |
|---|---|---|---|---|---|---|
| Abhinav Sarkar | `abhinav.sarkar@` | Territory/Area Sales Manager | 1 | **9** | 293 | **₹0.50 Cr** |
| Akshay Siraswar | `akshay.siraswar@` | Regional/Zonal Sales Manager | 3 | **21** | 424 | **₹0.63 Cr** |
| Shivam Pandey | `shivam.pandey@` | National Sales Manager | 12 | **95** | 3,061 | **₹13.71 Cr** |
| Neeraj Kumar | `neeraj.kumar@` | Head Office | 17 | **121** | 3,662 | **₹15.89 Cr** |

Containment holds: **9 ⊂ 21 ⊂ 95 ⊂ 121**.

**Who actually manages what** (the four real identities sit at different levels,
so only one of them is a *territory* owner):

| Territory | Territory Manager | Zonal | National |
|---|---|---|---|
| WSSTTY1 | **Abhinav Sarkar** | Akshay | Shivam |
| WSSTTY2 | Viraj Tiwari *(synthetic)* | Akshay | Shivam |
| WSSTTY3 | Rushil Saini *(synthetic)* | Akshay | Shivam |

Only WSSTTY1 has a real login as its Territory Manager. WSSTTY2 and WSSTTY3
keep their generated managers — Akshay covers all three as Zonal Manager.

Territories each one covers:

| Persona | Codes | Chains visible |
|---|---|---|
| Abhinav | WSSTTY1 | Sales only |
| Akshay | WSSTTY1, 2, 3 | Sales only |
| Shivam | WSSTTY1–12 | Sales only |
| Neeraj | all 17 rows | **Sales *and* MDI** |

---

## ⭐ The two questions worth building the demo around

### 1. "Who manages territory WSSTTY3?"

One question that proves row-level security **and** the dual-hierarchy modelling
at the same time.

| Asked by | Expected answer |
|---|---|
| **Abhinav** | **Nothing visible** — he is on WSSTTY1. Genie should say there is no data visible to him, *not* that the territory doesn't exist |
| **Akshay** | **1 row** — WSSTTY3 / Sales Hierarchy: Territory Manager **Rushil Saini**, under Akshay Siraswar, under Shivam Pandey. The MDI row for the same code is filtered out |
| **Shivam** | **1 row** — the same Sales Hierarchy row |
| **Neeraj** | **2 rows** — Sales Hierarchy (Rushil Saini → Akshay → Shivam) *and* MDI Hierarchy (**Nathaniel Sami → Saumya Mall → Udant Dewan**). Same code, two entirely separate management chains |

That last cell is the payoff: the same territory code, two management chains,
and only Head Office can see both.

### 2. "Compare the Sales Hierarchy and the MDI Hierarchy by revenue"

| Asked by | Expected answer |
|---|---|
| Abhinav | Sales Hierarchy only — ₹0.50 Cr, 9 dealers |
| Akshay | Sales Hierarchy only — ₹0.63 Cr, 21 dealers |
| Shivam | Sales Hierarchy only — ₹13.71 Cr, 95 dealers |
| **Neeraj** | **Both** — Sales ₹13.71 Cr / 93 dealers, MDI ₹2.18 Cr / 26 dealers |

The entire MDI business line is **invisible** to three of the four personas. Not
hidden by a dashboard filter — it does not exist in their query results.

---

## ✅ Verified against the live space

Asked through the deployed Genie space as **Akshay** and checked against the
CSVs. Every figure matched exactly.

**"Who manages territory WSSTTY3?"**

> Territory Manager: Rushil Saini · Zonal Manager: Akshay Siraswar ·
> National Manager: Shivam Pandey — Consumer & Bazaar, Sales Hierarchy

Correct, and note what is *absent*: the MDI Hierarchy row for the same code
was filtered out, because it is outside his scope. He cannot tell it exists.

**"Sales for August 2026"** — Genie volunteered the anchoring itself:

> *"The dataset's most recent data is through August 31, 2026, so I'm
> reporting August 2026 as the latest complete month."*

| Metric | Genie | Ground truth |
|---|---|---|
| Total Revenue | ₹2.34 L | **₹2.34 L** (233,730.06) |
| Total Quantity | 691 units | **691** |
| Transactions | 18 | **18** |
| Active Dealers | 7 | **7** (of his 21-dealer scope) |

Four instructions demonstrably firing at once: the **date anchor** (today is
7 September, so `current_date()` would have returned an empty September), the
**composite territory key**, **no self-filtering**, and closing with *"these
figures reflect your visible scope"* rather than presenting them as company
totals.

---

## Per-persona question sets

### Abhinav Sarkar — Territory/Area Sales Manager

| # | Question | Expected |
|---|---|---|
| 1 | What is my total revenue and how many dealers do I cover? | **₹0.50 Cr**, **9 dealers**, 293 transactions |
| 2 | Who are my top 5 dealers by revenue? | #115 Rau Enterprises **₹41.01 L**, #60 Walia Agencies ₹2.87 L, #116 Nori Paints & Hardware ₹2.25 L, #100 Solanki Paints & Hardware ₹0.81 L, #94 Khanna Enterprises ₹0.80 L |
| 3 | How many dormant dealers do I have? | **0** |
| 4 | Which product category sells the most for me? | **Sealants**, ₹0.27 Cr |
| 5 | Break my revenue down by division | **Consumer & Bazaar only**, ₹0.50 Cr |
| 6 | Which month was my best? | **April 2025**, ₹3.85 L |
| 7 | How many territories do I cover? | **1** — WSSTTY1 |
| 8 | Who manages territory WSSTTY3? | Nothing visible to him |

Note on Q2: one dealer carries **82%** of his territory. That is the Pareto
model working as intended, but be ready for the question — it looks lopsided
until you explain it.

### Akshay Siraswar — Regional/Zonal Sales Manager

| # | Question | Expected |
|---|---|---|
| 1 | What is my total revenue and how many dealers do I cover? | **₹0.63 Cr**, **21 dealers**, 424 transactions |
| 2 | Who are my top 5 dealers? | #115 Rau Enterprises ₹41.01 L, #108 Pandya Paints & Hardware ₹3.53 L, #27 Choudhury Enterprises ₹3.10 L, #60 Walia Agencies ₹2.87 L, #116 Nori Paints & Hardware ₹2.25 L |
| 3 | How many dormant dealers do I have? | **3** |
| 4 | How is each of my territories trending over the last 12 months? | 3 territories × months — WSSTTY1, WSSTTY2, WSSTTY3, all Sales Hierarchy |
| 5 | Which product category sells the most for me? | **Sealants**, ₹0.35 Cr |
| 6 | Which month was my best? | **November 2025**, ₹4.83 L |
| 7 | Break my revenue down by division | **Consumer & Bazaar only**, ₹0.63 Cr |
| 8 | Who manages territory WSSTTY3? | 1 row, Sales Hierarchy — **Rushil Saini** → Akshay → Shivam. ✅ *verified live* |

**Q1 against Abhinav's Q1 is the demo moment**: identical question, ₹0.50 Cr
becomes ₹0.63 Cr, 9 dealers becomes 21 — and Abhinav's dealers are a strict
subset of Akshay's.

### Shivam Pandey — National Sales Manager

| # | Question | Expected |
|---|---|---|
| 1 | What is my total revenue and how many dealers do I cover? | **₹13.71 Cr**, **95 dealers**, 3,061 transactions |
| 2 | Who are my top 5 dealers? | #38 Misra Enterprises ₹129.47 L, #103 Ray Traders ₹119.91 L, #39 Tripathi Building Materials ₹116.77 L, #13 Dua Hardware Store ₹112.53 L, #3 Sangha Hardware Store ₹112.38 L |
| 3 | Break my revenue down by division | Industrial Resins **₹5.62 Cr**, Construction Chemicals **₹3.85 Cr**, Waterproofing Solutions **₹3.62 Cr**, Consumer & Bazaar **₹0.63 Cr** |
| 4 | How many dormant dealers do I have? | **23** |
| 5 | Which product category sells the most? | **Construction Chemicals**, ₹5.25 Cr |
| 6 | Which month was my best? | **April 2025**, ₹93.66 L |
| 7 | How many territories do I cover, and which is largest? | **12**, all Sales Hierarchy |
| 8 | Compare Sales and MDI Hierarchy | **Sales only** — MDI is not visible to him |

### Neeraj Kumar — Head Office

| # | Question | Expected |
|---|---|---|
| 1 | What is total revenue and how many dealers are there? | **₹15.89 Cr**, **121 dealers**, 3,662 transactions |
| 2 | Who are the top 5 dealers? | #38 Misra Enterprises ₹129.47 L, #10 Chand Traders ₹122.94 L, #103 Ray Traders ₹119.91 L, #39 Tripathi Building Materials ₹116.77 L, #13 Dua Hardware Store ₹112.53 L |
| 3 | Break revenue down by division | Industrial Resins **₹7.03 Cr**, Construction Chemicals **₹4.24 Cr**, Waterproofing Solutions **₹3.94 Cr**, Consumer & Bazaar **₹0.68 Cr** |
| 4 | Compare Sales and MDI Hierarchy | Sales **₹13.71 Cr / 93 dealers**, MDI **₹2.18 Cr / 26 dealers** |
| 5 | How many dormant dealers are there? | **28** |
| 6 | Which product category sells the most? | **Industrial Resins**, ₹5.97 Cr |
| 7 | Which month was the best? | **May 2026**, ₹107.05 L |
| 8 | Who manages territory WSSTTY3? | **2 rows** — Sales: Rushil Saini → Akshay → Shivam; MDI: Nathaniel Sami → Saumya Mall → Udant Dewan |
| 9 | Which cities generate the most revenue? | ranked list across all 12 cities |

Notice Q2: **#10 Chand Traders appears for Neeraj but not for Shivam** — that
dealer sits in the MDI chain, which Shivam cannot see. A clean, concrete way to
show the boundary.

---

## 🔴 Known weak spots — check these before the client sees them

Genie is a language model over SQL. These are the places it is most likely to
get this dataset wrong, so test them deliberately rather than discovering them
live.

| Risk | What goes wrong | Mitigation |
|---|---|---|
| **Relative dates** | Anchors on `current_date()` → returns nothing, because the data ends 2026-08-31 | Space instructions say to anchor on `max(transaction_date)`. **Verify** with "last quarter" and "last 3 months" |
| **Fiscal year** | Treats "this year" as Jan–Dec instead of Apr–Mar | Instructions cover it. Test "how did we do this year" and check which window it used |
| **Summing the two chains** | Adds Sales + MDI into a single total without labelling | Instructions forbid it. Test "what is total revenue by hierarchy" |
| **Adding a user predicate** | Writes `WHERE user_email = ...` and double-filters to zero rows | Instructions forbid it explicitly. Test any "my ..." question |
| **Territory code alone** | Joins on `field_team_code` without `hierarchy_type`, merging two territories | Test "who manages WSSTTY3" as Neeraj — a single row instead of two means it got this wrong |
| **Invented metrics** | Answers a target/quota/margin question by substituting revenue | Instructions forbid it. Test "did I hit my target this quarter" — the right answer is "that data is not available" |

### ⚠️ A data problem that will surface *through* Genie

Ask "which is our biggest division" and the honest answer from this data is
**Industrial Resins (₹7.03 Cr)**, with **Consumer & Bazaar smallest at
₹0.68 Cr**.

For the real company that is backwards — Consumer & Bazaar is their largest
business. The generator prices consumer products low (Adhesives ₹350, Art &
Craft ₹180) without giving them the volume to match, so the division looks tiny.

Genie will state this confidently and the client's sales head will notice
immediately. Either **fix the volumes in `generate_sales.py`** before the demo,
or **avoid division-comparison questions** and say up front that relative
division sizes are not modelled. The first option is roughly ten minutes of work
and is the better one.

---

## Running the benchmark eval

The space ships with 12 benchmark questions (question + expected SQL) in
`genie/pidilite_demo.geniespace.json`, so accuracy can be measured rather than
eyeballed:

```bash
databricks genie genie-create-eval-run  --profile pidilite   # kick off a run
databricks genie genie-list-eval-runs   --profile pidilite   # find the run
databricks genie genie-list-eval-results --profile pidilite  # per-question results
```

Ad-hoc single question:

```bash
databricks genie ask "Who are my top 5 dealers by revenue?" --profile pidilite
```

⚠️ The eval and `ask` run **as whoever the profile authenticates as**. The
`pidilite` profile is Shadab, who is *not* in the access map — so he sees zero
rows and every answer will look empty. To test a persona, use a profile
authenticated as that persona, or ask from the Genie UI while signed in as them.
