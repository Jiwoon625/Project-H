"""Match EMAC catchments to apartments and route to each linked school over OSM ways."""
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
    def __init__(self):
        super().__init__()
        self.ids = {}
        self.points = []
        self.edges = {}
        self.major = set()
        self.crossings = set()
        self.gates = {}
        self.compounds = []
        self.steps = set()
        self.inclines = {}

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
        if node.tags.get("barrier") in {"gate", "lift_gate", "swing_gate"} or node.tags.get("entrance") in {"yes", "main"}:
            if node.location.valid() and 126.72 < node.lon < 127.23 and 37.31 < node.lat < 37.75:
                self.gates[node.id] = xy(node.lon, node.lat)
        if node.tags.get("highway") == "crossing":
            self.crossings.add(node.id)

    def way(self, way):
        school = way.tags.get("amenity") == "school"
        housing = way.tags.get("landuse") == "residential" and way.tags.get("name")
        if (school or housing) and way.is_closed() and len(way.nodes) >= 4:
            gates = [self.gates[n.ref] for n in way.nodes if n.ref in self.gates]
            if gates and all(n.location.valid() for n in way.nodes):
                polygon = Polygon([xy(n.lon, n.lat) for n in way.nodes])
                if polygon.is_valid:
                    self.compounds.append((polygon, "school" if school else "home", gates, way.tags.get("name", "")))
        road = way.tags.get("highway")
        if road not in WALKABLE or way.tags.get("area") == "yes":
            return
        foot = way.tags.get("foot")
        if foot in {"no", "private"} or (foot is None and
                (road == "cycleway" or way.tags.get("access") in {"no", "private"})):
            return
        previous = None
        for n in way.nodes:
            if not n.location.valid():
                previous = None
                continue
            lon, lat = n.lon, n.lat
            if not (126.72 < lon < 127.23 and 37.31 < lat < 37.75):
                previous = None
                continue
            idx = self.ids.get(n.ref)
            if idx is None:
                idx = len(self.points)
                self.ids[n.ref] = idx
                self.points.append(xy(lon, lat))
            if road in MAJOR:
                self.major.add(n.ref)
            if previous is not None and previous != idx:
                a, b = sorted((idx, previous))
                if road == "steps":
                    self.steps.add((a,b))
                slope = way.tags.get("incline", "").strip("%")
                try:
                    if slope:
                        self.inclines[a,b] = abs(float(slope))
                except ValueError:
                    pass
                self.edges[a, b] = math.dist(self.points[a], self.points[b])
            previous = idx

    def network(self):
        starts, ends = np.asarray(list(self.edges), dtype=np.int32).T
        matrix = coo_matrix((list(self.edges.values()), (starts, ends)),
                            shape=(len(self.points), len(self.points))).tocsr()
        return matrix + matrix.T


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    apartments = json.loads(Path("data.json").read_text())["complexes"]
    schools, pairs = assign(args.inputs, apartments)
    roads = WalkGraph()
    roads.apply_file(str(args.inputs / "roads.osm.pbf"), locations=True, idx="flex_mem")
    roads.areas = STRtree([a[0] for a in roads.compounds])
    network = roads.network()
    crossings = {roads.ids[k] for k in roads.crossings & roads.major}
    tree = cKDTree(roads.points)
    result = defaultdict(list)
    for school_id, homes in pairs.items():
        school = schools[school_id]
        lon, lat = float(school["경도"]), float(school["위도"])
        school_point, school_gate = roads.gate(xy(lon, lat), "school", school["학교명"])
        to_road, to_node = tree.query(school_point)
        distance, prev = dijkstra(network, indices=to_node, limit=6000, return_predecessors=True)
        for home in homes:
            origin, home_gate = roads.gate(xy(home["lng"], home["lat"]), "home", home["n"])
            from_road, node = tree.query(origin)
            steps, slope = False, None
            walk = count = None
            if max(to_road, from_road) <= 120 and np.isfinite(distance[node]):
                walk = round(from_road + distance[node] + to_road)
                count = int(node in crossings)
                while node != to_node:
                    edge = tuple(sorted((node, prev[node])))
                    steps = steps or edge in roads.steps
                    if edge in roads.inclines:
                        slope = max(slope or 0, roads.inclines[edge])
                    node = prev[node]
                    count += int(node in crossings)
            result[home["c"]].append([school["학교명"], walk, count, home_gate, school_gate, steps, slope])
    args.output.write_text(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
