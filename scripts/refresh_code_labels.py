#!/usr/bin/env python3
"""Regenerate plugins/anchorage_checkbook/schema/code_labels.json.

Why this file exists: FY2026 rows store Fund, G_L_Account and
Business_Area as BARE CODES ('101000', '0000200050', '8700') where every
earlier year stores 'code : label' strings and department names. The
plugin needs a code -> label map to resolve FY2026 rows, to make a label
filter (fund="Roads", business_area="Fire") reach them, and to merge the
two storage forms of one fund into one group.

Where each map comes from -- nothing here is typed in by hand:

* Fund / G_L_Account: every 'code : label' value on tables 0, 1 and 4,
  read one fiscal year at a time so the most recent label wins when a
  code was renamed.
* Business_Area: no table carries code and name side by side. But the
  payroll rollup (table 2) keeps department NAMES for FY2026 while the
  payroll line items (table 1) carry CODES, and the rollup is the sum of
  the line items. Matching the per-code and per-name FY2026 totals to
  the cent gives an exact crosswalk; the script refuses to write one
  that is not 1:1.

Run from the repo root after any upstream republish:

    python scripts/refresh_code_labels.py

It prints the FY2026 codes that still have no label (new funds or
accounts with no earlier-year row) and rewrites the JSON in place.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

SERVICE = (
    "https://services2.arcgis.com/Ce3DhLRthdwbHlfF/arcgis/rest/services/"
    "MOA_OpenCheckbook_Hosted/FeatureServer"
)
OUT = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "anchorage_checkbook"
    / "schema"
    / "code_labels.json"
)
SEP = " : "
CODED = {
    0: ("Fund", "G_L_Account"),
    1: ("Fund", "G_L_Account"),
    4: ("Fund", "G_L_Account"),
}

client = httpx.Client(timeout=60)


def query(table, params):
    data = client.get(f"{SERVICE}/{table}/query", params={"f": "json", **params}).json()
    if "error" in data:
        raise RuntimeError(f"table {table}: {data['error']}")
    return data


def distinct(table, field, where):
    values, offset = [], 0
    while True:
        data = query(
            table,
            {
                "where": where,
                "outFields": field,
                "returnDistinctValues": "true",
                "returnGeometry": "false",
                "orderByFields": field,
                "resultOffset": str(offset),
                "resultRecordCount": "2000",
            },
        )
        feats = data.get("features") or []
        values += [f["attributes"].get(field) for f in feats]
        if not feats or not data.get("exceededTransferLimit"):
            return values
        offset += len(feats)


def sums(table, field, measure, where):
    data = query(
        table,
        {
            "where": where,
            "groupByFieldsForStatistics": field,
            "outStatistics": json.dumps(
                [
                    {
                        "statisticType": "sum",
                        "onStatisticField": measure,
                        "outStatisticFieldName": "s",
                    }
                ]
            ),
        },
    )
    return {
        f["attributes"][field]: round(f["attributes"]["s"] or 0, 2)
        for f in data.get("features") or []
    }


def bare_code(value):
    text = value.strip() if isinstance(value, str) else ""
    return (text.lstrip("0") or "0") if text.isdigit() else None


def main():
    labels = {"Fund": {}, "G_L_Account": {}}
    bare = {"Fund": set(), "G_L_Account": set()}
    for table, fields in CODED.items():
        years = sorted(y for y in distinct(table, "Fiscal_Year", "1=1") if y)
        for field in fields:
            for year in years:  # ascending: the latest label wins
                for value in distinct(table, field, f"Fiscal_Year = '{year}'"):
                    if isinstance(value, str) and SEP in value:
                        code, label = value.split(SEP, 1)
                        labels[field][code.strip().lstrip("0") or "0"] = label.strip()
                    elif bare_code(value):
                        bare[field].add(bare_code(value))

    for field in labels:
        missing = sorted(bare[field] - set(labels[field]))
        print(
            f"{field}: {len(labels[field])} labelled codes; "
            f"{len(bare[field])} bare codes seen, {len(missing)} without a label"
        )
        if missing:
            print("   unlabelled:", ", ".join(missing))

    by_code = sums(1, "Business_Area", "Amount", "Fiscal_Year = '2026'")
    by_name = sums(2, "Business_Area", "Total_Payroll_Cost", "Fiscal_Year = '2026'")
    by_code = {k: v for k, v in by_code.items() if bare_code(k)}
    by_name = {k: v for k, v in by_name.items() if not bare_code(k)}
    name_of_total = {}
    for name, total in by_name.items():
        if total in name_of_total:
            sys.exit(f"two departments share the payroll total {total}; no 1:1 match")
        name_of_total[total] = name
    business_area = {}
    for code, total in sorted(by_code.items()):
        if total not in name_of_total:
            sys.exit(
                f"Business_Area code {code} (payroll {total}) matches no "
                f"department in the rollup; refusing to write a partial map"
            )
        business_area[bare_code(code)] = name_of_total[total]
    if len(set(business_area.values())) != len(business_area):
        sys.exit("Business_Area crosswalk is not 1:1")
    print(f"Business_Area: {len(business_area)} codes matched to the cent")
    for code, name in business_area.items():
        print(f"   {code} = {name}")

    pub = query(0, {"where": "1=1", "outFields": "PubDate", "resultRecordCount": "1"})
    stamp = pub["features"][0]["attributes"]["PubDate"]
    snapshot = datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d"
    )

    payload = {
        "_comment": (
            "GENERATED by scripts/refresh_code_labels.py -- do not edit by "
            "hand. Code -> label maps for the fields FY2026 stores as bare "
            "codes. Business_Area is derived by matching FY2026 payroll "
            "totals per code (table 1) to the rollup per name (table 2)."
        ),
        "snapshot": snapshot,
        "Fund": dict(sorted(labels["Fund"].items())),
        "G_L_Account": dict(sorted(labels["G_L_Account"].items())),
        "Business_Area": business_area,
    }
    OUT.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {OUT} (snapshot {snapshot})")


if __name__ == "__main__":
    main()
