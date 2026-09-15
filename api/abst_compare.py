"""
A/B Street 미시 시뮬레이션 전/후 비교 — 표준 라이브러리만.

engine이 output한 마크다운의 '★ 다구간 조합 최적안' 표를 파싱해,
network(suncheon_network.json) links와 seg_id/direction으로 매칭한 뒤
A/B Street headless 서버에 edits를 보내 baseline과 edited를 각각
09:00까지 진행 후 Drive 완료통행의 건수·평균초·p90초·전환도로 통과량 합을 비교한다.

반환: dict(ok, applied, skipped, before, after, delta, note)
실패 시 ok=False, before/after/delta는 None. 전체 60초 제한.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.request
import urllib.parse
from pathlib import Path

ABST_API = os.environ.get("ABST_API", "http://127.0.0.1:1234").rstrip("/")
SCENARIO = "data/system/zz/oneshot/scenarios/suncheon.osm/random.bin"
TIME_LIMIT = 60.0

_DIRECTION_TABLE = ["북", "북동", "동", "남동", "남", "남서", "서", "북서"]


# ────────────────────────────────────────────── 마크다운 표 파싱
def _parse_designation_table(markdown: str) -> list[dict]:
    """'## ★ 다구간 조합 최적안' 절에서 데이터 행의 두 번째 칸을 정규식으로 파싱해
    (name, seg, direction) 행을 추출한다.

    절 경계: "## ★ 다구간 조합 최적안" 부터 다음 "\\n## " 직전까지.
    데이터 행 예: "장평로 944-49구간 북행" → name="장평로", seg="944-49", direction="북행"
    정규식: r'^(.+?)\\s(\\d{3}-\\d{2})구간\\s(\\S+?)행$'
    """
    if not markdown:
        return []
    marker = "## ★ 다구간 조합 최적안"
    idx = markdown.find(marker)
    if idx < 0:
        return []
    start = idx + len(marker)
    # 다음 "## " 절 경계 찾기
    end = markdown.find("\n## ", start)
    if end < 0:
        end = len(markdown)
    section = markdown[start:end]
    out: list[dict] = []
    pat = re.compile(r'^(.+?)\s(\d{3}-\d{2})구간\s(\S+?)행$')
    for line in section.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("|"):
            # 마크다운 표 데이터 행: | rank | 지정명칭 | ... |
            cells = [c.strip() for c in s.split("|")]
            # cells[0]은 빈 문자열(표 시작 |), cells[1]이 rank, cells[2]가 지정명칭
            if len(cells) < 3:
                continue
            rank_txt = cells[1]
            if not rank_txt.isdigit():
                continue
            rank = int(rank_txt)
            name_cell = cells[2]
            m = pat.match(name_cell)
            if not m:
                continue
            name, seg, direction = m.group(1).strip(), m.group(2), m.group(3)
            if not name or not seg:
                continue
            out.append({"name": name, "seg": seg, "direction": direction, "rank": rank})
            continue
        m = pat.match(s)
        if not m:
            continue
        name, seg, direction = m.group(1).strip(), m.group(2), m.group(3)
        if not name or not seg:
            continue
        out.append({"name": name, "seg": seg, "direction": direction})
    return out


# ────────────────────────────────────────────── seg_id / direction 재현
def _direction(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """coord[u]->coord[v] 방위를 8방위로. road_impact.py direction() 재현."""
    dlat = lat2 - lat1
    dlon = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    if abs(dlat) < 1e-9 and abs(dlon) < 1e-9:
        return ""
    ang = (math.degrees(math.atan2(dlon, dlat)) + 360) % 360
    return _DIRECTION_TABLE[int((ang + 22.5) // 45) % 8]


def _seg_id(lat: float, lon: float) -> str:
    """coord[u] 기준 seg_id() 재현."""
    return f"{abs(lat) * 1000 % 1000:03.0f}-{abs(lon) * 100 % 100:02.0f}"


# ────────────────────────────────────────────── network 매핑
def _match_links(markdown: str, network_path: str) -> list[dict]:
    """마크다운 지정 행을 suncheon_network.json links와 매칭한다.

    도로명이 같고, coord[u] 기준 seg_id()와 direction()이 표의 seg/방향과
    일치하는 링크를 찾는다. 여러 개면 첫 매칭 사용.
    """
    net = json.loads(Path(network_path).read_text(encoding="utf-8"))
    coord = net["coord"]
    links = net.get("links") or []
    rows = _parse_designation_table(markdown)
    if not rows:
        return []
    matched: list[dict] = []
    for row in rows:
        name = row["name"]
        want_seg = row["seg"]
        want_dir = row["direction"]
        for ln in links:
            if ln.get("name") != name:
                continue
            u = ln.get("u")
            v = ln.get("v")
            if u is None or v is None:
                continue
            cu = coord.get(str(u))
            cv = coord.get(str(v))
            if cu is None or cv is None:
                continue
            lat_u, lon_u = cu
            lat_v, lon_v = cv
            got_seg = _seg_id(lat_u, lon_u)
            got_dir = _direction(lat_u, lon_u, lat_v, lon_v)
            if got_seg == want_seg and got_dir == want_dir:
                matched.append({
                    "rank": row["rank"],
                    "link": ln,
                    "u": u,
                    "v": v,
                    "lat_u": lat_u,
                    "lon_u": lon_u,
                    "lat_v": lat_v,
                    "lon_v": lon_v,
                    "way": ln.get("way"),
                    "name": name,
                })
                break
    return matched


# ────────────────────────────────────────────── A/B Street API 호출
def _post(path: str, payload: dict | None, timeout: float = 30.0) -> dict | None:
    url = f"{ABST_API}{path}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
        return json.loads(raw)
    except Exception:
        return None


def _get(path: str, timeout: float = 30.0) -> dict | None:
    url = f"{ABST_API}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
        return json.loads(raw)
    except Exception:
        return None


# ────────────────────────────────────────────── 스레드 안전장치
import threading as _threading
_LOCK = _threading.Lock()


# ────────────────────────────────────────────── edits 빌드
def _build_edits(matched: list[dict]) -> tuple | None:
    """전환 대상 링크들의 edits JSON을 만든다.

    반환: (edits_dict | None, applied_list, skipped_list) 튜플.
    commands가 비면 (None, [], skipped).

    각 링크의 중점으로 /map/get-nearest-road 호출 — 응답은 정수(road_id). dict 아님.
    osm_way_id는 /map/get-edit-road-command 응답의 r.osm_way_id로 확인한다.
    /map/get-all-geometry 피처에는 i1/i2 속성이 없다. properties.type=="intersection"인
    피처의 properties.id가 "https://www.openstreetmap.org/node/<id>"이고 geometry가
    Polygon이다. node id → 폴리곤 좌표 평균(중심점) 딕셔너리를 만들고,
    r.i1, r.i2로 찾아서 Fwd 벡터 = center[i2] - center[i1]로 구한다.
    차로 판정: lane["lt"] == "Driving" 이고 lane["dir"] 이 "Fwd"/"Back".
    ChangeRoad의 old/new는 lanes 리스트가 아니라 EditRoad 객체 전체.
      old = cmd["ChangeRoad"]["new"] 그대로
      new = 그걸 deepcopy 해서 lanes_ltr만 바꾼 것
    edits_name은 "oneway_combo".
    """
    import copy

    if not matched:
        return None, [], []

    # get-all-geometry 한 번 → intersection 중심점 딕셔너리
    geom = _get("/map/get-all-geometry", timeout=30.0)
    if not isinstance(geom, dict):
        return None, [], []
    center: dict[str, dict] = {}  # node_id(str) -> {"lat", "lon"}
    for feat in geom.get("features") or []:
        props = feat.get("properties") or {}
        if props.get("type") != "intersection":
            continue
        pid = props.get("id")
        if not isinstance(pid, str):
            continue
        import re as _re
        m = _re.search(r"/node/(\d+)$", pid)
        if not m:
            continue
        node_id = m.group(1)
        geom2 = feat.get("geometry")
        if not isinstance(geom2, dict):
            continue
        coords = geom2.get("coordinates")
        if not isinstance(coords, list) or len(coords) < 1:
            continue
        # Polygon 좌표: GeoJSON은 [ring] 구조 — coords[0]이 외곽 링의 점 리스트
        ring = coords[0]
        if not isinstance(ring, list) or len(ring) < 3:
            continue
        # 링 점들의 평균(중심점)
        s_lat = s_lon = 0.0
        n = 0
        for c in ring:
            if isinstance(c, (list, tuple)) and len(c) >= 2:
                s_lon += float(c[0])
                s_lat += float(c[1])
                n += 1
        if n == 0:
            continue
        center[node_id] = {"lat": s_lat / n, "lon": s_lon / n}

    # get-all-geometry에서 type=="road" 피처(properties.id=".../way/<id>", Polygon)를 way id별로 수집
    road_polys: dict[str, list] = {}  # way_id(str) -> [Polygon coords, ...]
    for feat in geom.get("features") or []:
        props = feat.get("properties") or {}
        if props.get("type") != "road":
            continue
        pid = props.get("id")
        if not isinstance(pid, str):
            continue
        m2 = _re.search(r"/way/(\d+)$", pid)
        if not m2:
            continue
        wid = m2.group(1)
        geom2 = feat.get("geometry")
        if not isinstance(geom2, dict):
            continue
        coords = geom2.get("coordinates")
        if not isinstance(coords, list):
            continue
        road_polys.setdefault(wid, []).append(coords)

    # get-edits 템플릿 한 번
    edits_template = _get("/map/get-edits", timeout=30.0)
    if not isinstance(edits_template, dict):
        return None, [], []

    commands: list[dict] = []
    applied_list: list[dict] = []
    skipped_list: list[dict] = []

    for m in matched:
        ln = m["link"]
        way = m["way"]
        u = m["u"]
        v = m["v"]
        lat_u, lon_u = m["lat_u"], m["lon_u"]
        lat_v, lon_v = m["lat_v"], m["lon_v"]

        # 링크 중점
        mid_lat = (lat_u + lat_v) / 2
        mid_lon = (lon_u + lon_v) / 2

        nr = _get(f"/map/get-nearest-road?lat={mid_lat}&lon={mid_lon}&threshold_meters=60", timeout=20.0)
        # 응답은 정수(road_id) — dict 아님
        if nr is None or not isinstance(nr, int):
            skipped_list.append({"rank": m.get("rank", 0), "name": m["name"], "reason": "get-nearest-road 실패(정수 아님)"})
            continue
        road_id = nr

        cmd = _get(f"/map/get-edit-road-command?id={road_id}", timeout=20.0)
        if not isinstance(cmd, dict) or "ChangeRoad" not in cmd:
            skipped_list.append({"rank": m.get("rank", 0), "name": m["name"], "reason": "get-edit-road-command 실패"})
            continue

        cr = cmd["ChangeRoad"]
        r = cr.get("r") or {}
        osm_way_id = r.get("osm_way_id")
        if osm_way_id != way:
            # 바로 skip하지 말고, 해당 way의 도로 폴리곤 중심점으로 재시도
            polys = road_polys.get(str(way))
            if polys:
                best_dist = None
                best_lat = best_lon = None
                for poly_coords in polys:
                    ring = poly_coords[0] if isinstance(poly_coords, list) and len(poly_coords) >= 1 else poly_coords
                    if not isinstance(ring, list):
                        continue
                    s_lat = s_lon = 0.0
                    n = 0
                    for c in ring:
                        if isinstance(c, (list, tuple)) and len(c) >= 2:
                            s_lon += float(c[0])
                            s_lat += float(c[1])
                            n += 1
                    if n == 0:
                        continue
                    clat = s_lat / n
                    clon = s_lon / n
                    dlat = clat - mid_lat
                    dlon = (clon - mid_lon) * math.cos(math.radians((clat + mid_lat) / 2))
                    dist = dlat * dlat + dlon * dlon
                    if best_dist is None or dist < best_dist:
                        best_dist = dist
                        best_lat = clat
                        best_lon = clon
                if best_lat is not None:
                    nr2 = _get(f"/map/get-nearest-road?lat={best_lat}&lon={best_lon}&threshold_meters=30", timeout=20.0)
                    if nr2 is not None and isinstance(nr2, int):
                        cmd2 = _get(f"/map/get-edit-road-command?id={nr2}", timeout=20.0)
                        if isinstance(cmd2, dict) and "ChangeRoad" in cmd2:
                            cr2 = cmd2["ChangeRoad"]
                            r2 = cr2.get("r") or {}
                            osm_way_id2 = r2.get("osm_way_id")
                            if osm_way_id2 == way:
                                # 재시도 성공 — road_id와 cmd, r을 교체
                                road_id = nr2
                                cmd = cmd2
                                cr = cr2
                                r = r2
                                osm_way_id = osm_way_id2
            if osm_way_id != way:
                skipped_list.append({"rank": m.get("rank", 0), "name": m["name"], "reason": f"osm_way_id={osm_way_id} != way={way}"})
                continue

        new_lanes_obj = cr.get("new")
        if not isinstance(new_lanes_obj, dict):
            skipped_list.append({"rank": m.get("rank", 0), "name": m["name"], "reason": "new(EditRoad) 아님"})
            continue
        lanes_ltr = new_lanes_obj.get("lanes_ltr")
        if not isinstance(lanes_ltr, list):
            skipped_list.append({"rank": m.get("rank", 0), "name": m["name"], "reason": "lanes_ltr 없음"})
            continue

        # Fwd 벡터 = center[i2] - center[i1]
        i1 = r.get("i1")
        i2 = r.get("i2")
        if i1 is None or i2 is None:
            skipped_list.append({"rank": m.get("rank", 0), "name": m["name"], "reason": "r.i1/i2 없음"})
            continue
        c1 = center.get(str(i1))
        c2 = center.get(str(i2))
        if c1 is None or c2 is None:
            skipped_list.append({"rank": m.get("rank", 0), "name": m["name"], "reason": "geometry에 node id 없음"})
            continue
        fwd_lat = c2["lat"] - c1["lat"]
        fwd_lon = (c2["lon"] - c1["lon"]) * math.cos(math.radians((c1["lat"] + c2["lat"]) / 2))
        if fwd_lat == 0 and fwd_lon == 0:
            skipped_list.append({"rank": m.get("rank", 0), "name": m["name"], "reason": "Fwd 벡터 0"})
            continue

        # u->v 벡터
        uv_lat = lat_v - lat_u
        uv_lon = (lon_v - lon_u) * math.cos(math.radians((lat_u + lat_v) / 2))
        if uv_lat == 0 and uv_lon == 0:
            skipped_list.append({"rank": m.get("rank", 0), "name": m["name"], "reason": "u->v 벡터 0"})
            continue

        dot = uv_lat * fwd_lat + uv_lon * fwd_lon
        # 내적 양수 → u->v가 Fwd 방향, 음수 → Back 방향
        target_idx = None
        for i, lane in enumerate(lanes_ltr):
            if not isinstance(lane, dict):
                continue
            if lane.get("lt") != "Driving":
                continue
            if dot > 0 and lane.get("dir") == "Fwd":
                target_idx = i
                break
            if dot <= 0 and lane.get("dir") == "Back":
                target_idx = i
                break
        if target_idx is None:
            skipped_list.append({"rank": m.get("rank", 0), "name": m["name"], "reason": "제거할 Driving 차로 없음"})
            continue
        new_lanes_ltr = [dict(l) for l in lanes_ltr]
        del new_lanes_ltr[target_idx]
        if not new_lanes_ltr:
            skipped_list.append({"rank": m.get("rank", 0), "name": m["name"], "reason": "Driving 차로 전부 제거 → skip"})
            continue
        # old/new는 lanes_ltr이 아니라 EditRoad 객체 전체
        old_obj = new_lanes_obj  # cmd["ChangeRoad"]["new"] 그대로
        new_obj = copy.deepcopy(new_lanes_obj)
        new_obj["lanes_ltr"] = new_lanes_ltr
        commands.append({
            "ChangeRoad": {
                "r": {"osm_way_id": osm_way_id, "i1": i1, "i2": i2},
                "old": old_obj,
                "new": new_obj,
            }
        })
        applied_list.append({"rank": m.get("rank", 0), "name": m["name"]})

    if not commands:
        return None, [], skipped_list

    edits = dict(edits_template)
    edits.pop("commands", None)
    edits["commands"] = commands
    edits["edits_name"] = "oneway_combo"
    return edits, applied_list, skipped_list


# ────────────────────────────────────────────── 집계
def _aggregate(road_ids: set | None = None) -> dict | None:
    """GET /data/get-finished-trips + /data/get-road-thruput 집계.

    get-finished-trips는 리스트: 각 원소 {"id","person","duration","distance_crossed","mode"}.
      duration/10000 = 초.
    get-road-thruput은 {"counts": [[road_id, agent_type, hour, n]]}.
      agent_type 값이 "Drive"가 아니라 "Car".
    전환 도로 통과량은 applied 된 road_id 들만 합산 — road_ids(집합)를 넘겨 필터링.
    """
    trips = _get("/data/get-finished-trips", timeout=15.0)
    # 응답은 리스트
    if not isinstance(trips, list):
        return None
    drive_durs: list[float] = []
    for t in trips:
        if not isinstance(t, dict):
            continue
        if t.get("mode") != "Drive":
            continue
        d = t.get("duration")
        if d is None:
            continue
        try:
            secs = float(d) / 10000.0
        except (TypeError, ValueError):
            continue
        if secs > 0:
            drive_durs.append(secs)

    thruput = _get("/data/get-road-thruput", timeout=15.0)
    road_sum = 0
    if isinstance(thruput, dict):
        for row in (thruput.get("counts") or []):
            if not isinstance(row, list) or len(row) < 4:
                continue
            # [road_id, agent_type, hour, n]
            if row[1] != "Car":
                continue
            if road_ids is not None:
                try:
                    rid = int(row[0])
                except (TypeError, ValueError):
                    continue
                if rid not in road_ids:
                    continue
            try:
                road_sum += int(row[3])
            except (TypeError, ValueError):
                pass

    if not drive_durs:
        return {"trips": 0, "mean_sec": 0.0, "p90_sec": 0.0, "road_sum": road_sum}

    drive_durs.sort()
    n = len(drive_durs)
    mean_sec = sum(drive_durs) / n
    idx90 = int(math.ceil(0.90 * n)) - 1
    idx90 = max(0, min(idx90, n - 1))
    p90_sec = drive_durs[idx90]
    return {
        "trips": n,
        "mean_sec": round(mean_sec, 2),
        "p90_sec": round(p90_sec, 2),
        "road_sum": road_sum,
    }


# ────────────────────────────────────────────── 공개 API
def compare(markdown: str, network_path: str) -> dict:
    """engine 마크다운과 network 파일로 A/B Street 전/후 비교.

    network_path: api/ 기준 상대 경로 (예: 'suncheon_network.json').
    반환: {ok, applied, skipped, before, after, delta, note}
    """
    t0 = time.time()
    note = "A/B Street 무작위 수요 기준. 엔진과 수요가 달라 절대값 비교 금지, 방향성 교차검증용"

    with _LOCK:
        return _compare_impl(markdown, network_path, t0, note)

# ────────────────────────────────────────────── 내부 구현 (락 안에서 실행)
def _compare_impl(markdown: str, network_path: str, t0: float, note: str) -> dict:
    matched = _match_links(markdown, network_path)
    if not matched:
        return {
            "ok": False, "applied": 0, "skipped": 0,
            "before": None, "after": None, "delta": None,
            "note": note + " (전환 대상 매칭 실패)",
        }

    # edits 빌드 (한 번만 호출 — 내부 skip 목록을 축적)
    if time.time() - t0 > TIME_LIMIT:
        return {
            "ok": False, "applied": 0, "skipped": 0,
            "before": None, "after": None, "delta": None,
            "note": note + " (시간 초과: 매칭 후)",
        }
    edits, built_applied, built_skipped = _build_edits(matched)
    applied = built_applied
    skipped = built_skipped

    if time.time() - t0 > TIME_LIMIT:
        return {
            "ok": False, "applied": applied, "skipped": skipped,
            "before": None, "after": None, "delta": None,
            "note": note + " (시간 초과: edits 빌드 후)",
        }

    # baseline
    _post("/sim/load", {"scenario": SCENARIO, "modifiers": [], "edits": None}, timeout=30.0)
    _get("/sim/goto-time?t=09:00:00", timeout=15.0)
    applied_road_ids = {c["ChangeRoad"]["r"]["osm_way_id"] for c in (edits.get("commands") or []) if "ChangeRoad" in c and "r" in c["ChangeRoad"]}
    before = _aggregate(applied_road_ids if applied_road_ids else None)
    if before is None:
        return {
            "ok": False, "applied": applied, "skipped": skipped,
            "before": None, "after": None, "delta": None,
            "note": note + " (baseline 집계 실패)",
        }
    if time.time() - t0 > TIME_LIMIT:
        return {
            "ok": False, "applied": applied, "skipped": skipped,
            "before": before, "after": None, "delta": None,
            "note": note + " (시간 초과: baseline 후)",
        }

    # edited
    _post("/sim/load", {"scenario": SCENARIO, "modifiers": [], "edits": edits}, timeout=30.0)
    _get("/sim/goto-time?t=09:00:00", timeout=15.0)
    after = _aggregate()
    if after is None:
        return {
            "ok": False, "applied": applied, "skipped": skipped,
            "before": before, "after": None, "delta": None,
            "note": note + " (edited 집계 실패)",
        }

    delta = {
        "trips": after["trips"] - before["trips"],
        "mean_sec": round(after["mean_sec"] - before["mean_sec"], 2),
        "p90_sec": round(after["p90_sec"] - before["p90_sec"], 2),
        "road_sum": after["road_sum"] - before["road_sum"],
    }
    return {
        "ok": True,
        "applied": applied,
        "skipped": skipped,
        "before": before,
        "after": after,
        "delta": delta,
        "note": note,
    }
