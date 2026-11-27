"""Compute latest departures for a 09:00 arrival using KTDB bus and metro connections."""
import argparse
import csv
import io
import json
import math
from pathlib import Path
from zipfile import ZipFile

import numpy as np
from scipy.spatial import cKDTree

# Representative destinations, not the boundaries of employment districts.
HUBS = {"광화문광장": (37.5720, 126.9769), "강남역": (37.4979, 127.0276),
        "여의도역": (37.5216, 126.9243), "가산디지털단지역": (37.4816, 126.8826),
        "판교테크노밸리 유스페이스": (37.4005, 127.1065), "범계역": (37.3898, 126.9508)}
DEADLINE = 9 * 3600
START = 6 * 3600
WALK_MPS = 1.2
DETOUR = 1.3
TRANSFER = 180


def xy(lat, lon):
    return ((lon - 127) * 88300, (lat - 37.5) * 111100)


def walking(distance):
    return int(math.ceil(distance * DETOUR / WALK_MPS))


def rows(feed, name):
    return csv.DictReader(io.TextIOWrapper(feed.open(name + ".txt"), encoding="utf-8-sig"))


def seconds(value):
    h, m, s = map(int, value.split(b":"))
    return h * 3600 + m * 60 + s


def load_feed(feed):
    stops = {}
    for r in rows(feed, "stops"):
        lat, lon = float(r["stop_lat"]), float(r["stop_lon"])
        if 37.18 < lat < 37.83 and 126.60 < lon < 127.33:
            stops[r["stop_id"].encode()] = xy(lat, lon)
    ids = {key: i for i, key in enumerate(stops)}
    routes = {r["route_id"] for r in rows(feed, "routes") if r["route_type"] in {"0", "1"}}
    weekday = {r["service_id"] for r in rows(feed, "calendar") if r["wednesday"] == "1"}
    trips = {r["trip_id"].encode(): i for i, r in enumerate(rows(feed, "trips"))
             if r["route_id"] in routes and r["service_id"] in weekday}
    connections = []
    previous = None
    # KTDB supplies stop times grouped by trip and ordered by stop_sequence.
    with feed.open("stop_times.txt") as times:
        next(times)
        for line in times:
            trip, arr, dep, stop, seq, pickup, drop, _ = line.rstrip().split(b",")
            if stop not in ids or trip not in trips or not b"06:" <= arr < b"09:01":
                previous = None
                continue
            origin = ids[stop]
            if previous and previous[0] == trip:
                _, seq0, dep0, from_stop, can_board = previous
                if int(seq) > seq0 and dep0 < seconds(arr) <= DEADLINE:
                    connections.append((dep0, seconds(arr), from_stop, origin, trips[trip],
                                        can_board, drop == b"0"))
            previous = trip, int(seq), seconds(dep), origin, pickup == b"0"
    connections.sort(reverse=True)
    return np.array(list(stops.values())), connections, max(trips.values()) + 1


def latest(trips_count, connections, near, destination, points, tree):
    # best_board excludes transfer slack. ready includes the walk and transfer at alighting.
    best_board = np.full(len(points), -1, dtype=int)
    ready = best_board.copy()
    for i in tree.query_ball_point(destination, 900):
        ready[i] = DEADLINE - walking(math.dist(points[i], destination))
    onboard = bytearray(trips_count)
    for departure, arrival, start, end, trip, pickup, drop in connections:
        if not onboard[trip] and not (drop and ready[end] >= arrival):
            continue
        onboard[trip] = 1
        if not pickup or departure <= best_board[start]:
            continue
        best_board[start] = departure
        for neighbour, delay in near[start]:
            value = departure - delay
            if value > ready[neighbour]:
                ready[neighbour] = value
    return best_board


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("feed", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    apartments = json.loads(Path("data.json").read_text())["complexes"]
    with ZipFile(args.feed) as feed:
        points, connections, trip_count = load_feed(feed)
    tree = cKDTree(points)
    near = [[(j, TRANSFER + walking(math.dist(points[i], points[j]))) for j in group]
            for i, group in enumerate(tree.query_ball_point(points, 250))]
    origins = {}
    for c in apartments:
        if c["lat"] is None or c["lng"] is None:
            continue
        origin = xy(c["lat"], c["lng"])
        origins[c["c"]] = (origin, [(i, 120 + walking(math.dist(origin, points[i])))
                                     for i in tree.query_ball_point(origin, 900)])
    result = {key: [] for key in origins}
    for lat, lon in HUBS.values():
        dest = xy(lat, lon)
        board = latest(trip_count, connections, near, dest, points, tree)
        for key, (origin, nearby) in origins.items():
            candidates = [DEADLINE - int(board[i]) + walk for i, walk in nearby if board[i] >= 0]
            direct = math.dist(origin, dest)
            if direct <= 2000:
                candidates.append(walking(direct))
            best = min(candidates, default=DEADLINE)
            result[key].append(math.ceil(best / 60) if best <= DEADLINE - START else None)
    args.output.write_text(json.dumps({"hubs": list(HUBS), "times": result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
