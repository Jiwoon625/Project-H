"""Match EMAC catchments to apartments and route to each linked school over OSM ways.

Memory-lean variant of tools/school_routes.py for a subset of complexes on a small machine.
It keeps coordinates only for OSM nodes near the apartments and their schools, builds the
walking graph from ways that stay inside that area, and otherwise follows the same rules
(gates, crossings, steps, incline).
"""
import argparse
import csv
import io
import json
import math
from collections import defaultdict
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import osmium
import shapefile
from pyproj import Transformer
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree
from shapely import Point, Polygon, STRtree
from shapely.geometry import shape

WALKABLE = set("footway pedestrian path steps living_street residential service unclassified "
               "tertiary secondary primary road track cycleway".split())
MAJOR = {"primary", "secondary", "tertiary"}
REACH = 3200          # 단지·학교에서 이 거리 안의 보행로만 그래프에 넣는다


def xy(lon, lat):
    return (lon - 127) * 88300, (lat - 37.5) * 111100


def csv_records(path):
    with ZipFile(path) as archive:
        with io.TextIOWrapper(archive.open(archive.namelist()[0]), encoding="utf-8-sig") as file:
            return list(csv.DictReader(file))


def assign(root, apartments):
    schools = {r["학교ID"]: r for r in csv_records(root / "schools")
               if r["학교급구분"] == "초등학교" and r["운영상태"] == "운영"}
    links = defaultdict(set)
    for r in csv_records(root / "links"):
        if r["학교ID"] in schools:
            links[r["학구ID"]].add(r["학교ID"])
    zones, ids = [], []
    with ZipFile(root / "zones") as archive:
        def part(ext):
            return archive.read(next(n for n in archive.namelist() if n.endswith("." + ext)))
        project = Transformer.from_crs(4326, part("prj").decode(), always_xy=True)
        reader = shapefile.Reader(shp=io.BytesIO(part("shp")), dbf=io.BytesIO(part("dbf")),
                                  shx=io.BytesIO(part("shx")), encoding="cp949")
        for rec in reader.iterShapeRecords():
            if rec.record.SD_CD not in {"11", "41"}:
                continue
            zones.append(shape(rec.shape.__geo_interface__))
            ids.append(rec.record.HAKGUDO_ID)
    tree = STRtree(zones)
    pairs = defaultdict(list)
    for c in apartments:
        if c["lat"] is None or c["lng"] is None:
            continue
        p = Point(*project.transform(c["lng"], c["lat"]))
        school_ids = set()
        for i in tree.query(p, predicate="intersects"):
            school_ids.update(links[ids[i]])
        for school in sorted(school_ids):
            pairs[school].append(c)
    return schools, pairs


class WalkGraph(osmium.SimpleHandler):
    """보행망 부분 그래프. 좌표는 targets 반경 REACH 안의 노드만 보관한다."""

    def __init__(self, targets):
        super().__init__()
        self.tree = cKDTree(targets) if len(targets) else None
        self.ids = {}
        self.px, self.py = [], []
        self.ea, self.eb, self.ew = [], [], []
        self.major = set()
        self.crossings = set()
        self.gates = {}
        self.compounds = []
        self.steps = set()
        self.inclines = {}

    def near(self, x, y):
        if self.tree is None:
            return False
        return self.tree.query_ball_point((x, y), REACH, return_length=True) > 0

    def idx_of(self, ref, lon, lat):
        i = self.ids.get(ref)
        if i is None:
            i = len(self.px)
            self.ids[ref] = i
            x, y = xy(lon, lat)
            self.px.append(x)
            self.py.append(y)
        return i

    def gate(self, point, kind, name):
        matches = self.areas.query(Point(point), predicate="intersects")
        choices = [gate for i in matches for gate in self.compounds[i][2]
                   if self.compounds[i][1] == kind and
                   (kind == "home" or not self.compounds[i][3] or
                    name.removeprefix("서울") in self.compounds[i][3] or
                    self.compounds[i][3] in name)]
        if not choices:
            return point, False
        return min(choices, key=lambda g: math.dist(point, g)), True

    def node(self, node):
        if not node.location.valid():
            return
        lon, lat = node.lon, node.lat
        if not (126.72 < lon < 127.30 and 37.28 < lat < 37.78):
            return
        if not self.near(*xy(lon, lat)):
            return
        self.idx_of(node.id, lon, lat)   # 반경 안 노드만 좌표를 보관(전국 노드 캐시를 피한다)
        tags = node.tags
        if tags.get("barrier") in {"gate", "lift_gate", "swing_gate"} or tags.get("entrance") in {"yes", "main"}:
            self.gates[node.id] = xy(lon, lat)
        if tags.get("highway") == "crossing":
            self.crossings.add(node.id)

    def way(self, way):
        school = way.tags.get("amenity") == "school"
        housing = way.tags.get("landuse") == "residential" and way.tags.get("name")
        members = list(way.nodes)
        if not members:
            return
        if school or housing:
            # 단지·학교 울타리는 대상 근처 노드만 보관하므로, 모든 노드 좌표가 있을 때만 쓴다.
            if way.is_closed() and len(members) >= 4 and all(n.ref in self.ids for n in members):
                gates = [self.gates[n.ref] for n in members if n.ref in self.gates]
                if gates:
                    polygon = Polygon([(self.px[self.ids[n.ref]], self.py[self.ids[n.ref]]) for n in members])
                    if polygon.is_valid:
                        self.compounds.append((polygon, "school" if school else "home", gates, way.tags.get("name", "")))
            return
        road = way.tags.get("highway")
        if road not in WALKABLE or way.tags.get("area") == "yes":
            return
        if any(n.ref not in self.ids for n in members):
            return
        foot = way.tags.get("foot")
        if foot in {"no", "private"} or (foot is None and
                (road == "cycleway" or way.tags.get("access") in {"no", "private"})):
            return
        slope = None
        raw = way.tags.get("incline", "").strip("%")
        if raw:
            try:
                slope = abs(float(raw))
            except ValueError:
                slope = None
        previous = None
        for n in members:
            idx = self.ids[n.ref]
            if road in MAJOR:
                self.major.add(n.ref)
            if previous is not None and previous != idx:
                a, b = sorted((idx, previous))
                if road == "steps":
                    self.steps.add(a * 10_000_000 + b)
                if slope:
                    self.inclines[a * 10_000_000 + b] = slope
                self.ea.append(a)
                self.eb.append(b)
                self.ew.append(math.dist((self.px[a], self.py[a]), (self.px[b], self.py[b])))
            previous = idx

    def network(self):
        n = len(self.px)
        starts = np.asarray(self.ea, dtype=np.int32)
        ends = np.asarray(self.eb, dtype=np.int32)
        vals = np.asarray(self.ew, dtype=np.float64)
        # 보행로에 실제로 쓰인 노드만 스냅 대상으로 둔다(건물·부지 외곽선 노드에 붙는 것 방지)
        self.road_ids = np.unique(np.concatenate([starts, ends])) if len(starts) else np.array([], dtype=np.int32)
        matrix = coo_matrix((vals, (starts, ends)), shape=(n, n)).tocsr()
        return matrix + matrix.T

    def points(self):
        return np.column_stack([np.asarray(self.px), np.asarray(self.py)])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    apartments = json.loads(Path("data.json").read_text())["complexes"]
    schools, pairs = assign(args.inputs, apartments)
    progress = Path(str(args.output) + ".schools.json")
    result = defaultdict(list)
    done = set()
    if args.output.exists():
        try:
            result.update(json.loads(args.output.read_text()))
        except Exception:
            result = defaultdict(list)
    if progress.exists():
        try:
            done = set(json.loads(progress.read_text()))
        except Exception:
            done = set()
    todo = {k: v for k, v in pairs.items() if k not in done}
    print(f"schools {len(pairs)} · 남은 {len(todo)} · 이미 {len(done)}", flush=True)
    targets = [xy(c["lng"], c["lat"]) for c in apartments if c["lat"] is not None]
    for school in schools.values():
        if school["학교ID"] in todo:
            targets.append(xy(float(school["경도"]), float(school["위도"])))
    roads = WalkGraph(targets)
    roads.apply_file(str(args.inputs / "roads.osm.pbf"), locations=False)
    roads.areas = STRtree([a[0] for a in roads.compounds])
    points = roads.points()
    network = roads.network()
    crossing_idx = {roads.ids[k] for k in roads.crossings if k in roads.ids and k in roads.major}
    road_points = points[roads.road_ids]
    tree = cKDTree(road_points)

    def snap(point):
        d, i = tree.query(point)
        return d, int(roads.road_ids[i])
    print(f"graph nodes {len(points)} edges {len(roads.ea)} compounds {len(roads.compounds)}", flush=True)
    for n_done, (school_id, homes) in enumerate(sorted(todo.items()), 1):
        school = schools[school_id]
        lon, lat = float(school["경도"]), float(school["위도"])
        school_point, school_gate = roads.gate(xy(lon, lat), "school", school["학교명"])
        to_road, to_node = snap(school_point)
        distance, prev = dijkstra(network, indices=to_node, limit=6000, return_predecessors=True)
        for home in homes:
            origin, home_gate = roads.gate(xy(home["lng"], home["lat"]), "home", home["n"])
            from_road, node = snap(origin)
            steps, slope = False, None
            walk = count = None
            if max(to_road, from_road) <= 120 and np.isfinite(distance[node]):
                walk = round(from_road + distance[node] + to_road)
                count = int(node in crossing_idx)
                while node != to_node:
                    p = prev[node]
                    if p < 0:
                        break
                    edge = tuple(sorted((int(node), int(p))))
                    key = edge[0] * 10_000_000 + edge[1]
                    steps = steps or key in roads.steps
                    if key in roads.inclines:
                        slope = max(slope or 0, roads.inclines[key])
                    node = p
                    count += int(node in crossing_idx)
            result[home["c"]].append([school["학교명"], walk, count, home_gate, school_gate, steps, slope])
        done.add(school_id)
        if n_done % 10 == 0:
            args.output.write_text(json.dumps(result, ensure_ascii=False))
            progress.write_text(json.dumps(sorted(done), ensure_ascii=False))
            print(f"  {n_done}/{len(todo)}", flush=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False))
    progress.write_text(json.dumps(sorted(done), ensure_ascii=False))
    print("schools", len(pairs), "homes", sum(len(v) for v in pairs.values()),
          "graph nodes", len(points), "edges", len(roads.ea))


if __name__ == "__main__":
    main()
