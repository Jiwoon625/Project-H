#!/usr/bin/env python3
"""Build access.js from the final data.json plus the previous travel data and the new areas.

- existing complexes: keep the travel times, school routes and verified contract months already
  published in access.js (identical snapshot, unchanged coordinates)
- new complexes: take values computed for them (commute_new.json / routes_new.json)
사용: python3 tools/merge_access.py inputs_new/commute_new.json inputs_new/routes_new.json
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_existing():
    path = ROOT / "access.js"
    if not path.exists():
        return {"snapshot": None, "hubs": [], "homes": {}}
    text = path.read_text()
    return json.loads(text[text.index("=") + 1:].rstrip().rstrip(";"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("commute", type=Path)
    parser.add_argument("routes", type=Path)
    args = parser.parse_args()
    data = json.loads((ROOT / "data.json").read_text())
    old = load_existing()
    commute = json.loads(args.commute.read_text())
    routes = json.loads(args.routes.read_text())
    result = {"snapshot": data["generated"], "hubs": commute["hubs"], "homes": {}}
    kept = new = 0
    for c in data["complexes"]:
        code, lat, lng = c["c"], c["lat"], c["lng"]
        prev = old["homes"].get(code)
        same_place = prev and prev.get("lat") == lat and prev.get("lng") == lng
        if same_place and prev.get("times"):
            times, kept = prev["times"], kept + 1
        else:
            times, new = commute["times"].get(code, []), new + 1
        schools = prev["schools"] if same_place and prev.get("schools") else routes.get(code, [])
        months = prev.get("months", {}) if (same_place and old.get("snapshot") == data["generated"]) else {}
        result["homes"][code] = {"lat": lat, "lng": lng, "times": times, "schools": schools, "months": months}
    (ROOT / "access.js").write_text("const ACCESS = " + json.dumps(result, ensure_ascii=False,
                                                                separators=(",", ":")) + ";\n")
    print(f"homes {len(result['homes'])} · 기존 유지 {kept} · 신규 {new}")


if __name__ == "__main__":
    main()
