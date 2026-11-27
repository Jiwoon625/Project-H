"""Package computed travel indicators and transaction months reconciled to this snapshot."""
import argparse
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from statistics import median

def name_key(value):
    return re.sub(r"[^0-9a-z가-힣]", "", value.lower()).removesuffix("아파트")


def last_months(data, sales):
    # A month belongs to a particular snapshot, apartment and size. Only publish verified matches.
    districts = {d["c"]: d["n"] for d in data["districts"] if d["s"] == "서울"}
    homes, candidates = defaultdict(list), defaultdict(list)
    output = defaultdict(dict)
    for c in data["complexes"]:
        if c["si"] == "서울":
            homes[c["g"], c["d"], name_key(c["n"]), c["y"]].append(c)
    period = data["points"][-1]
    year, start, end = re.fullmatch(r"(\d{4})-(\d{2})~(\d{2})", period).groups()
    with sqlite3.connect(f"file:{sales}?mode=ro", uri=True) as connection:
        rows = connection.execute("""
            SELECT sgg_cd,umd,apt_name,build_year,apt_seq,area,price,deal_date
            FROM trades WHERE canceled=0 AND deal_ym BETWEEN ? AND ? AND deal_date <= ?
            ORDER BY deal_date
        """, (year+start, year+end, data["generated"][:10]))
        for district, dong, name, built, apt_seq, size, price, date in rows:
            if size is None or price is None or apt_seq is None:
                continue
            area = 59 if 59 <= size < 60 else 84 if 84 <= size < 85 else None
            key = (districts.get(district), dong, name_key(name), built)
            if area and len(homes.get(key, [])) == 1:
                candidates[homes[key][0]["c"], area].append((apt_seq, price, date))
    by_id = {c["c"]: c for c in data["complexes"]}
    for (code, area), trades in candidates.items():
        c = by_id[code]
        if (len({t[0] for t in trades}) == 1 and len(trades) == c[f"n{area}"]
                and median(t[1] for t in trades) == c[f"p{area}"]):
            output[code][area] = max(t[2] for t in trades)[:7]
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", type=Path)
    args = parser.parse_args()
    data = json.loads(Path("data.json").read_text())
    root = args.inputs
    commute = json.loads((root / "commute.json").read_text())
    routes = json.loads((root / "routes.json").read_text())
    months = last_months(data, (root / "sales.db").resolve())
    result = {"snapshot": data["generated"], "hubs": commute["hubs"], "homes": {}}
    for c in data["complexes"]:
        code = c["c"]
        result["homes"][code] = {"lat": c["lat"], "lng": c["lng"],
                                 "times": commute["times"].get(code, []),
                                 "schools": routes.get(code, []), "months": months.get(code, {})}
    Path("access.js").write_text("const ACCESS = " + json.dumps(result, ensure_ascii=False,
                                separators=(",", ":")) + ";\n")


if __name__ == "__main__":
    main()
