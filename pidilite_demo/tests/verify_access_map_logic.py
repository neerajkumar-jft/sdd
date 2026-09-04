"""
Pre-flight check for the gold access-map logic, runnable with no workspace.

This mirrors the entitlement algebra in src/pidilite_demo/gold.py (and silver's
validity rules) in plain Python against the generated CSVs, then asserts the
properties row-level security depends on:

  1. containment    - Master's customers subset of RA1's subset of RA2's subset of Head Office's
  2. no cross-leak  - a code shared by both chains never hands one dealer to both
  3. coverage       - every real Master has at least one customer (a persona with
                      an empty dashboard kills the live demo)
  4. no identity    - a roster row with no usable email gets no entitlement at all

IMPORTANT: this validates the *logic*, not Unity Catalog enforcement. It cannot
tell you whether a row filter survives a pipeline update, or whether the
pipeline's own identity gets filtered. Those still need the workspace checks.
"""
import csv
import os
import re
import sys
from collections import defaultdict

BASE = os.path.join(os.path.dirname(__file__), "..", "sample_data")

HIERARCHY_MAP = {
    "SALES HIERARCHY": "Sales Hierarchy",
    "SALESHIERARCHY": "Sales Hierarchy",
    "MDI HIERARCHY": "MDI Hierarchy",
    "MDIHIERARCHY": "MDI Hierarchy",
    "MDI": "MDI Hierarchy",
    "ALL": "All",
}
ROLE_MAP = {
    "HEAD OFFICE": "Head Office",
    "HO": "Head Office",
    "TERRITORY/AREA SALES MANAGER": "Territory/Area Sales Manager",
    "REGIONAL/ZONAL SALES MANAGER": "Regional/Zonal Sales Manager",
    "NATIONAL SALES MANAGER": "National Sales Manager",
}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HEAD_OFFICE_ROLE = "Head Office"

MANAGEMENT_SCOPES = [
    ("master_person_id", "Territory/Area Sales Manager"),
    ("ra1_person_id", "Regional/Zonal Sales Manager"),
    ("ra2_person_id", "National Sales Manager"),
]


def read(entity):
    with open(os.path.join(BASE, entity, f"{entity}.csv"), newline="") as f:
        return list(csv.DictReader(f))


def canon(value, mapping):
    key = " ".join(str(value or "").strip().upper().replace("_", " ").replace("-", " ").split())
    return mapping.get(key, str(value or "").strip())


# --- silver-equivalent cleansing + validity ---------------------------------

divisions = {}
for r in read("division"):
    try:
        divisions[int(r["division_id"])] = r["division_name"].strip()
    except (TypeError, ValueError):
        pass

persons = {}
for r in read("person"):
    email = r["user_email"].strip().lower()
    pid = r["person_id"].strip().upper()
    valid = bool(pid and r["person_name"].strip() and r["role"].strip()
                 and email and EMAIL_RE.match(email))
    persons[pid] = {
        "person_id": pid,
        "role": canon(r["role"], ROLE_MAP),
        "user_email": email,
        "valid": valid,
    }

field_teams = []
for r in read("field_team"):
    hierarchy = canon(r.get("hierarchy_type") or r.get("Tzxntyoe"), HIERARCHY_MAP)
    code = r["field_team_code"].strip().upper()
    try:
        division_id = int(r["division_id"])
    except (TypeError, ValueError):
        division_id = None
    valid = bool(code and division_id in divisions and hierarchy
                 and r["master_person_id"].strip())
    field_teams.append({
        "field_team_code": code,
        "hierarchy_type": hierarchy,
        "division_id": division_id,
        "master_person_id": r["master_person_id"].strip().upper(),
        "ra1_person_id": r["ra1_person_id"].strip().upper(),
        "ra2_person_id": r["ra2_person_id"].strip().upper(),
        "valid": valid,
    })

valid_ft_keys = {(f["field_team_code"], f["hierarchy_type"]) for f in field_teams if f["valid"]}

customers = []
for r in read("customer"):
    try:
        code = int(r["customer_code"])
    except (TypeError, ValueError):
        continue
    key = (r["field_team_code"].strip().upper(), canon(r["hierarchy_type"], HIERARCHY_MAP))
    valid = bool(r["customer_name"].strip() and key[0] and key[1] and key in valid_ft_keys)
    customers.append({"customer_code": code, "key": key, "valid": valid})

# --- gold-equivalent access maps --------------------------------------------

ft_grants = set()   # (person_id, code, hierarchy, via_role)
for f in field_teams:
    if not f["valid"]:
        continue
    for column, via_role in MANAGEMENT_SCOPES:
        pid = f[column]
        if pid:
            ft_grants.add((pid, f["field_team_code"], f["hierarchy_type"], via_role))

ho_people = [p for p in persons.values() if p["role"] == HEAD_OFFICE_ROLE and p["valid"]]
for p in ho_people:
    for code, hierarchy in sorted(valid_ft_keys):
        ft_grants.add((p["person_id"], code, hierarchy, HEAD_OFFICE_ROLE))

# inner join on the roster: no usable email -> no entitlement
access_map_field_team = {
    (persons[pid]["user_email"], pid, via, code, hierarchy)
    for pid, code, hierarchy, via in ft_grants
    if pid in persons and persons[pid]["valid"]
}

cust_by_key = defaultdict(list)
for c in customers:
    if c["valid"]:
        cust_by_key[c["key"]].append(c["customer_code"])

access_map_customer = {
    (email, pid, via, cust)
    for email, pid, via, code, hierarchy in access_map_field_team
    for cust in cust_by_key[(code, hierarchy)]
}

seen = defaultdict(set)
for email, pid, via, cust in access_map_customer:
    seen[pid].add(cust)

# --- assertions --------------------------------------------------------------

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}{('  -> ' + detail) if detail else ''}")
    if not condition:
        failures.append(label)


print("access_map_field_team rows:", len(access_map_field_team))
print("access_map_customer rows:  ", len(access_map_customer))
print("valid customers:           ", sum(1 for c in customers if c['valid']),
      f"of {len(customers)}")
print("\n1. containment (Master subset of RA1 subset of RA2 subset of HO)")
ho_union = set().union(*[seen[p["person_id"]] for p in ho_people]) if ho_people else set()
for f in field_teams:
    if not f["valid"] or not cust_by_key[(f["field_team_code"], f["hierarchy_type"])]:
        continue
    m, r1, r2 = seen[f["master_person_id"]], seen[f["ra1_person_id"]], seen[f["ra2_person_id"]]
    label = f"{f['field_team_code']}/{f['hierarchy_type'][:5]}"
    if not (m <= r1 <= r2 <= ho_union):
        check(f"containment {label}", False,
              f"master={len(m)} ra1={len(r1)} ra2={len(r2)} ho={len(ho_union)}")
print(f"  PASS  all {len(valid_ft_keys)} valid field teams satisfy Master <= RA1 <= RA2 <= HO"
      if not failures else "")

print("\n2. no cross-hierarchy leak")
by_code = defaultdict(set)
for code, hierarchy in valid_ft_keys:
    by_code[code].add(hierarchy)
shared = {c: h for c, h in by_code.items() if len(h) > 1}

# The precise property is about the *customer sets* behind a shared code.
# Comparing the two Masters' total entitlements instead would be muddied by
# anyone who also holds a Head Office grant - they legitimately see everything,
# so an "overlap" there is correct behaviour, not a leak.
keys_per_customer = defaultdict(set)
for c in customers:
    if c["valid"]:
        keys_per_customer[c["customer_code"]].add(c["key"])
ambiguous = {k: sorted(v) for k, v in keys_per_customer.items() if len(v) > 1}
check("every customer resolves to exactly one (code, hierarchy)", not ambiguous, str(ambiguous))

for code, hierarchies in sorted(shared.items()):
    sets = {h: set(cust_by_key[(code, h)]) for h in hierarchies}
    left, right = sorted(sets)
    overlap = sets[left] & sets[right]
    check(f"{code}: {left} ({len(sets[left])} cust) vs {right} ({len(sets[right])} cust)",
          not overlap, f"OVERLAP={sorted(overlap)}" if overlap else "disjoint")

# Master-level sanity, skipping anyone who is also Head Office.
ho_ids = {p["person_id"] for p in ho_people}
for code, hierarchies in sorted(shared.items()):
    ms = []
    for h in sorted(hierarchies):
        ft = next(f for f in field_teams if f["valid"]
                  and f["field_team_code"] == code and f["hierarchy_type"] == h)
        ms.append((ft["master_person_id"], seen[ft["master_person_id"]]))
    if any(m[0] in ho_ids for m in ms):
        print(f"  SKIP  {code} masters - one of them also holds Head Office scope")
        continue
    overlap = ms[0][1] & ms[1][1]
    check(f"{code} masters {ms[0][0]} vs {ms[1][0]}", not overlap,
          f"OVERLAP={sorted(overlap)}" if overlap else "disjoint")

print("\n3. coverage - every real Master has at least one customer")
empty = [f"{f['field_team_code']}/{f['hierarchy_type']}"
         for f in field_teams
         if f["valid"] and not f["master_person_id"].startswith("P9")
         and not seen[f["master_person_id"]]]
check("no real Master has an empty dashboard", not empty, str(empty))

print("\n4. no identity -> no entitlement")
no_email = [p["person_id"] for p in persons.values() if not p["valid"]]
leaked = [pid for pid in no_email if seen.get(pid)]
check(f"quarantined roster rows excluded ({len(no_email)} found)", not leaked, str(leaked))

print("\n5. via_role recorded for the reverse lookup")
roles = sorted({via for _, _, via, _, _ in access_map_field_team})
check("all four scope levels present", len(roles) == 4, str(roles))

print("\n" + ("FAILURES: " + ", ".join(failures) if failures else "ALL CHECKS PASSED"))
sys.exit(1 if failures else 0)
