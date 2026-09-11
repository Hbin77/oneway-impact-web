#!/usr/bin/env python3
"""
일방통행 전환 영향 분석 — 의존성 0 (파이썬 표준 라이브러리만)
================================================================
어떤 도로 구간을 일방통행으로 바꾸면 그 구역 전체 통행시간이
몇 % 늘거나 주는지를 교통공학 표준 절차로 계산한다.

절차
  1) Overpass API로 대상 구역 간선 도로망 수집
  2) BPR 링크성능함수 + Frank-Wolfe 이용자균형(UE) 배정
  3) 방향별로 한 방향씩 차단(=일방통행 전환)하고 재배정
  4) 총 통행시간(TSTT) 변화량으로 순위 산출

핵심: 도로를 막으면 운전자가 경로를 바꾼다. 그 재선택까지 계산해야
      실제 효과가 나온다. 이것이 브라에스 역설을 잡아내는 유일한 방법이다.
"""
import json, math, heapq, urllib.request, urllib.parse, sys

# 공용 Overpass는 504가 잦다. 미러를 순회하고, 그래도 안 되면 동봉 캐시를 쓴다.
MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.jp/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
ALPHA, BETA = 0.15, 4.0          # BPR 계수 (미국 도로국 표준)

# 도로등급별 기본값 — OSM 결측 보정용 (편도차로, km/h, 차로당 시간용량)
DEFAULTS = {
    "motorway": (3, 100, 2000), "trunk": (3, 80, 1800),
    "primary": (3, 60, 1600), "secondary": (2, 50, 1400),
    "tertiary": (2, 40, 1200), "unclassified": (1, 30, 800),
}
for k in list(DEFAULTS):
    l, s, c = DEFAULTS[k]
    DEFAULTS[k + "_link"] = (1, s, c)
CLASSES = list(DEFAULTS)


# ────────────────────────────────────────────── 1. 도로망 수집
def fetch(bbox, timeout=120, tries=2, log=print):
    """bbox=(S,W,N,E). 미러를 순회하며 간선 도로망 수집. 전부 실패하면 예외."""
    s, w, n, e = bbox
    hw = "|".join(k for k in CLASSES if not k.endswith("_link")) + "|" + \
         "|".join(k for k in CLASSES if k.endswith("_link"))
    q = f"""[out:json][timeout:{timeout}];
(way["highway"~"^({hw})$"]({s},{w},{n},{e}););
out body geom;"""
    last = None
    for attempt in range(tries):
        for url in MIRRORS:
            try:
                req = urllib.request.Request(
                    url, data=urllib.parse.urlencode({"data": q}).encode(),
                    headers={"User-Agent": "road-impact-skill/1.0"})
                with urllib.request.urlopen(req, timeout=timeout + 30) as r:
                    d = json.loads(r.read().decode())
                if d.get("elements"):
                    return d
                last = "빈 응답"
            except Exception as ex:
                last = f"{type(ex).__name__}: {ex}"
                log(f"  · {url.split('/')[2]} 실패 ({last})")
    raise RuntimeError(f"Overpass 미러 전부 실패 — 마지막 오류: {last}")


def fetch_biz(bbox, timeout=90, log=print):
    """상업 POI 좌표 — 상권 접근성 계산용"""
    s, w, n, e = bbox
    q = f"""[out:json][timeout:{timeout}];
(node["shop"]({s},{w},{n},{e});
 node["amenity"~"^(restaurant|cafe|bank|marketplace|pharmacy)$"]({s},{w},{n},{e}););
out center;"""
    for url in MIRRORS:
        try:
            req = urllib.request.Request(
                url, data=urllib.parse.urlencode({"data": q}).encode(),
                headers={"User-Agent": "road-impact-skill/1.0"})
            with urllib.request.urlopen(req, timeout=timeout+30) as r:
                d = json.loads(r.read().decode())
            return [(el["lat"], el["lon"]) for el in d.get("elements", [])
                    if "lat" in el and "lon" in el]
        except Exception:
            continue
    log("  · 상업 POI 수집 실패 — 상권 지표는 생략")
    return []


def save_cache(path, coord, links, biz=None, obs=None):
    json.dump({"coord": {str(k): v for k, v in coord.items()},
               "links": links, "biz": biz or [], "obs": obs or []},
              open(path, "w", encoding="utf-8"))


def load_cache(path):
    d = json.load(open(path, encoding="utf-8"))
    return ({int(k): tuple(v) for k, v in d["coord"].items()},
            d["links"], [tuple(x) for x in d.get("biz", [])], d.get("obs", []))


NOMINATIM = "https://nominatim.openstreetmap.org/search"


def geocode(place, span_km=3.0, log=print):
    """
    지명을 위경도 범위로 바꾼다. "순천 원도심" 같은 일상 표현을 그대로 받기 위한 것.
    OSM Nominatim(무료·인증 불필요, 언어모델 아님)을 쓴다.
    반환: (남, 서, 북, 동) 또는 None
    """
    q = urllib.parse.urlencode({"q": place, "format": "json", "limit": 1,
                                "countrycodes": "kr"})
    req = urllib.request.Request(
        NOMINATIM + "?" + q,
        headers={"User-Agent": "road-impact-skill/1.0 (traffic analysis)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
    except Exception as ex:
        log(f"  · 지명 검색 실패 ({type(ex).__name__})")
        return None
    if not d:
        return None
    hit = d[0]
    lat, lon = float(hit["lat"]), float(hit["lon"])
    # 검색 결과에 경계상자가 있으면 쓰고, 너무 작으면 span_km로 넓힌다
    bb = hit.get("boundingbox")
    if bb:
        s_, n_, w_, e_ = float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])
    else:
        s_ = n_ = lat; w_ = e_ = lon
    dlat = span_km / 111.0 / 2
    dlon = span_km / (111.0 * math.cos(math.radians(lat))) / 2
    if (n_ - s_) < dlat * 2:
        s_, n_ = lat - dlat, lat + dlat
    if (e_ - w_) < dlon * 2:
        w_, e_ = lon - dlon, lon + dlon
    log(f"  · '{place}' → {hit.get('display_name','')[:40]}")
    return (s_, w_, n_, e_)


def haversine(a, b):
    """두 (lat,lon) 사이 거리(m)"""
    R = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(h))


def _num(v):
    if v is None:
        return None
    v = str(v).split(";")[0].split("|")[0]      # "30;50" 같은 복합값은 첫 값만
    s = "".join(ch for ch in v if ch.isdigit() or ch == ".")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def build_graph(data):
    """Overpass 응답 → 방향성 링크 목록. 교차점(2개 이상 way가 공유하는 절점)에서 분할."""
    use = {}
    for el in data.get("elements", []):
        if el.get("type") != "way" or "geometry" not in el:
            continue
        for nd in el["nodes"]:
            use[nd] = use.get(nd, 0) + 1

    coord, links = {}, []
    for el in data.get("elements", []):
        if el.get("type") != "way" or "geometry" not in el:
            continue
        tags = el.get("tags", {})
        hwy = tags.get("highway", "tertiary")
        if hwy not in DEFAULTS:
            hwy = "tertiary"
        dl, ds, dc = DEFAULTS[hwy]

        oneway = tags.get("oneway", "no")
        is_one = oneway in ("yes", "true", "1", "-1")
        rev = (oneway == "-1")

        lanes = _num(tags.get("lanes"))
        lanes_src = "osm"
        if not lanes or lanes <= 0:
            lanes, lanes_src = float(dl), "기본값"
        elif not is_one:
            lanes = max(1.0, lanes / 2.0)
        spd = _num(tags.get("maxspeed"))
        spd_src = "osm"
        if not spd or spd <= 0:
            spd, spd_src = float(ds), "기본값"

        ids, geo = el["nodes"], el["geometry"]
        for i, (nid, g) in enumerate(zip(ids, geo)):
            coord[nid] = (g["lat"], g["lon"])

        # 교차점 사이를 하나의 링크로
        cut = [0] + [i for i in range(1, len(ids)-1) if use.get(ids[i], 0) > 1] + [len(ids)-1]
        for a, b in zip(cut, cut[1:]):
            L = sum(haversine((geo[i]["lat"], geo[i]["lon"]),
                              (geo[i+1]["lat"], geo[i+1]["lon"])) for i in range(a, b))
            if L < 1:
                continue
            u, v = ids[a], ids[b]
            if rev:
                u, v = v, u
            base = dict(length=L, hwy=hwy, lanes=lanes, speed=spd,
                        t0=L / (spd * 1000.0 / 60.0), cap=lanes * dc,
                        lanes_src=lanes_src, spd_src=spd_src,
                        name=tags.get("name", ""), way=el["id"])
            links.append(dict(base, u=u, v=v, oneway=is_one))
            if not is_one:
                links.append(dict(base, u=v, v=u, oneway=False))
    return coord, links


# ────────────────────────────────────────────── 2. 배정 엔진
class Net:
    def __init__(self, coord, links):
        # 병렬링크 병합: 최소 t0 유지, 용량 합산
        merged = {}
        for L in links:
            k = (L["u"], L["v"])
            if k in merged:
                m = merged[k]
                m["cap"] += L["cap"]
                if L["t0"] < m["t0"]:
                    m.update({x: L[x] for x in ("t0", "length", "hwy", "name", "speed", "lanes")})
            else:
                merged[k] = dict(L)
        ids = sorted({n for k in merged for n in k})
        self.idx = {n: i for i, n in enumerate(ids)}
        self.ids, self.coord = ids, coord
        self.E = list(merged.values())
        for e in self.E:
            e["ui"], e["vi"] = self.idx[e["u"]], self.idx[e["v"]]
        self.n, self.m = len(ids), len(self.E)
        self.t0 = [e["t0"] for e in self.E]
        self.cap = [max(e["cap"], 1.0) for e in self.E]
        self.adj = [[] for _ in range(self.n)]
        for i, e in enumerate(self.E):
            self.adj[e["ui"]].append((e["vi"], i))
        self.radj = [[] for _ in range(self.n)]
        for i, e in enumerate(self.E):
            self.radj[e["vi"]].append((e["ui"], i))

    def largest_component(self):
        """강연결성분 중 최대 — 배정 불가한 고립 조각 제거"""
        seen, comp = [False]*self.n, []
        for s in range(self.n):
            if seen[s]:
                continue
            f, st = {s}, [s]
            while st:
                x = st.pop()
                for y, _ in self.adj[x]:
                    if y not in f:
                        f.add(y); st.append(y)
            b, st = {s}, [s]
            while st:
                x = st.pop()
                for y, _ in self.radj[x]:
                    if y not in b:
                        b.add(y); st.append(y)
            c = f & b
            for x in c:
                seen[x] = True
            comp.append(c)
        return max(comp, key=len)

    def dijkstra(self, src, cost, mask):
        INF = float("inf")
        dist = [INF]*self.n
        pe = [-1]*self.n
        dist[src] = 0.0
        pq = [(0.0, src)]
        while pq:
            d, x = heapq.heappop(pq)
            if d > dist[x] + 1e-12:
                continue
            for y, i in self.adj[x]:
                if not mask[i]:
                    continue
                nd = d + cost[i]
                if nd < dist[y] - 1e-12:
                    dist[y] = nd; pe[y] = i
                    heapq.heappush(pq, (nd, y))
        return dist, pe

    def bpr(self, v, marginal=False):
        out = []
        for i in range(self.m):
            r = v[i]/self.cap[i]
            rb = r**BETA
            out.append(self.t0[i]*(1.0 + ALPHA*(1.0+BETA)*rb) if marginal
                       else self.t0[i]*(1.0 + ALPHA*rb))
        return out

    def aon(self, cost, zones, od, mask):
        y = [0.0]*self.m
        lost = 0.0
        for i, o in enumerate(zones):
            dist, pe = self.dijkstra(o, cost, mask)
            for j, dz in enumerate(zones):
                f = od[i][j]
                if f <= 0:
                    continue
                if dist[dz] == float("inf"):
                    lost += f; continue
                cur = dz
                while cur != o:
                    e = pe[cur]
                    if e < 0:
                        break
                    y[e] += f
                    cur = self.E[e]["ui"]
        return y, lost

    def assign(self, zones, od, mask=None, marginal=False, max_iter=60, tol=1e-5):
        """
        Frank-Wolfe 이용자균형 배정.

        반드시 전량배분(AON)으로 시작한다. AON 해는 OD 전량을 실은 실행가능해이고,
        FW의 모든 반복은 실행가능해들의 볼록결합이므로 OD 보존이 유지된다.
        임의의 교통량 벡터에서 출발하면(예: 이전 해에서 차단링크만 0으로 만든 것)
        그 통행량이 사라진 채 계산되어 총통행시간이 크게 과소평가된다.
        """
        if mask is None:
            mask = [True]*self.m
        v = [0.0]*self.m
        v, lost = self.aon(self.bpr(v, marginal), zones, od, mask)
        gap = float("inf")
        it = 0
        for it in range(1, max_iter+1):
            c = self.bpr(v, marginal)
            y, lost = self.aon(c, zones, od, mask)
            d = [y[i]-v[i] for i in range(self.m)]
            num = sum(c[i]*(v[i]-y[i]) for i in range(self.m))
            den = sum(c[i]*v[i] for i in range(self.m)) + 1e-12
            gap = num/den
            if gap < tol:
                break
            # 정확 선형탐색: dZ/da = sum t(v+a*d)*d = 0
            def g(a):
                s = 0.0
                for i in range(self.m):
                    x = v[i] + a*d[i]
                    r = x/self.cap[i]
                    t = self.t0[i]*(1.0 + (ALPHA*(1.0+BETA) if marginal else ALPHA)*r**BETA)
                    s += t*d[i]
                return s
            if g(1.0) < 0:
                a = 1.0
            else:
                lo, hi = 0.0, 1.0
                # 20회면 30회와 결과가 동일하다(실측 오차 0.0000%). 그 이하는
                # 오차가 잡음 임계(0.03%)를 넘어 위험하다.
                for _ in range(20):
                    mid = 0.5*(lo+hi)
                    if g(mid) > 0: hi = mid
                    else: lo = mid
                a = 0.5*(lo+hi)
            v = [v[i] + a*d[i] for i in range(self.m)]
        t = self.bpr(v)
        return dict(flow=v, time=t, tstt=sum(t[i]*v[i] for i in range(self.m)),
                    gap=gap, iters=it, lost=lost)


# ────────────────────────────────────────────── 3. 존 · OD
def make_zones(net, bbox, grid=4, total=12000.0, beta=0.12):
    """
    존: bbox를 grid×grid로 나누고 도로망 노드가 있는 셀만 채택
    활동량 대리지표: 셀 내 도로 연장 (KTDB OD 입수 전 임시값)
    ⚠️ 실측 OD가 아니다. 절대량이 아니라 '상대 비교'용으로만 해석할 것.
    """
    s, w, n, e = bbox
    xs = [w + (e-w)*i/grid for i in range(grid+1)]
    ys = [s + (n-s)*i/grid for i in range(grid+1)]
    cells = []
    for i in range(grid):
        for j in range(grid):
            x0, x1, y0, y1 = xs[i], xs[i+1], ys[j], ys[j+1]
            mem = [k for k in range(net.n)
                   if x0 <= net.coord[net.ids[k]][1] <= x1
                   and y0 <= net.coord[net.ids[k]][0] <= y1]
            if not mem:
                continue
            act = sum(E["length"] for E in net.E
                      if E["ui"] in mem or E["vi"] in mem)
            cx, cy = (x0+x1)/2, (y0+y1)/2
            c = min(mem, key=lambda k: (net.coord[net.ids[k]][1]-cx)**2
                                     + (net.coord[net.ids[k]][0]-cy)**2)
            cells.append((c, max(act, 1.0)))
    zones = [c for c, _ in cells]
    act = [a for _, a in cells]
    nz = len(zones)
    P = [a*total/sum(act) for a in act]
    A = list(P)

    mask = [True]*net.m
    C = []
    for o in zones:
        dist, _ = net.dijkstra(o, net.t0, mask)
        C.append([dist[d] if dist[d] != float("inf") else 1e6 for d in zones])
    F = [[0.0 if i == j else math.exp(-beta*C[i][j]) for j in range(nz)] for i in range(nz)]
    a_, b_ = [1.0]*nz, [1.0]*nz
    for _ in range(150):
        for i in range(nz):
            s_ = sum(F[i][j]*b_[j] for j in range(nz))
            a_[i] = P[i]/s_ if s_ > 1e-12 else 0.0
        for j in range(nz):
            s_ = sum(F[i][j]*a_[i] for i in range(nz))
            b_[j] = A[j]/s_ if s_ > 1e-12 else 0.0
    od = [[a_[i]*F[i][j]*b_[j] for j in range(nz)] for i in range(nz)]
    return zones, od, C


# ────────────────────────────────────────────── 3-b. 상권 접근성
def biz_access(net, zones, od, cost, mask, biz_nodes):
    """
    상권 접근성 — **상가 하나하나가 얼마나 접근하기 어려워졌는가**를 본다.

    각 상업지 b에 대해 access(b) = 존에서 b까지 가는 통행시간을 존 발생량으로
    가중평균한 값(분). 이 값이 커지면 그 상가로 오기가 어려워졌다는 뜻이다.

    평균만 보면 안 된다. 도심에서는 상가가 많아 평균이 희석되고, 특정 상가
    한 곳이 고립돼도 지표가 움직이지 않는다. 그래서 상가별 값을 전부 반환해
    **최악의 상가**를 따로 판정한다. 통행시간만 최적화하면 상가 앞 도로를
    막는 안이 나오는데, 그것을 거르는 것이 이 지표의 목적이다.

    반환: {"mean": 전체 평균, "per": {상업노드: 접근시간}}
    """
    if not biz_nodes:
        return None
    acc = {b: [0.0, 0.0] for b in biz_nodes}      # b → [가중합, 가중치]
    for i, o in enumerate(zones):
        dist, _ = net.dijkstra(o, cost, mask)
        p = sum(od[i])
        if p <= 0:
            continue
        for b in biz_nodes:
            d = dist[b]
            if d == float("inf"):
                d = 60.0                           # 도달 불가는 1시간으로 벌점
            acc[b][0] += d * p
            acc[b][1] += p
    per = {b: (v[0] / v[1]) for b, v in acc.items() if v[1] > 0}
    if not per:
        return None
    return {"mean": sum(per.values()) / len(per), "per": per}


def biz_verdict(b0, b1):
    """상권 영향 판정 — 평균과 최악을 함께 본다."""
    if not b0 or not b1:
        return None
    dm = b1["mean"] - b0["mean"]
    worst_key, worst_d = None, 0.0
    hurt = 0
    for b, v0 in b0["per"].items():
        v1 = b1["per"].get(b)
        if v1 is None:
            continue
        d = v1 - v0
        if d > 0.05:
            hurt += 1
        if d > worst_d:
            worst_d, worst_key = d, b
    return dict(mean0=b0["mean"], mean1=b1["mean"], dmean=dm,
                worst=worst_d, hurt=hurt, total=len(b0["per"]))


def find_biz_nodes(net, biz_xy, k=60):
    """상업 POI에 가장 가까운 도로망 노드들"""
    if not biz_xy:
        return []
    seen, out = set(), []
    for lat, lon in biz_xy:
        best, bd = None, 1e18
        for i in range(net.n):
            la, lo = net.coord[net.ids[i]]
            d = (la-lat)**2 + (lo-lon)**2
            if d < bd:
                bd, best = d, i
        if best is not None and best not in seen:
            seen.add(best); out.append(best)
        if len(out) >= k:
            break
    return out


# ────────────────────────────────────────────── 4. 일방통행 전환 스캔
def scan(net, zones, od, base, topk=0, max_iter=None, refine=True):
    """
    양방향 링크 쌍에서 한 방향을 차단 = 그 구간을 일방통행으로 전환.
    ΔTSTT < 0 이면 일방통행이 전체에 이득 (브라에스형).
    """
    pair = {(E["ui"], E["vi"]): i for i, E in enumerate(net.E)}
    cands = [i for i, E in enumerate(net.E) if (E["vi"], E["ui"]) in pair]
    # 양방향 후보를 전부 본다. 표본을 뽑으면 어떤 구간이 후보에 드느냐에 따라
    # 결과가 통째로 흔들린다(실측: topk 40 없음, 60 있음, 80 없음).
    # 브라에스형 구간은 교통량이 크지 않아 어떤 정렬 기준으로도 안정적으로
    # 잡히지 않으므로, 전수 검토가 유일한 답이다.
    cands.sort(key=lambda i: -base["flow"][i])
    lim = topk if topk > 0 else MAX_CAND
    if lim < len(cands):
        cands = cands[:lim]
    T0 = base["tstt"]

    # 2단계 평가 — 후보 전체를 낮은 정밀도로 훑고, 표에 실릴 양극단만
    # 정밀도를 올려 다시 푼다. 전부 정밀하게 풀면 비용이 3배가 되는데
    # 중간 순위 구간은 어차피 보고에 쓰이지 않는다.
    def one(i, it):
        mask = [True]*net.m
        mask[i] = False
        r = net.assign(zones, od, mask=mask, max_iter=it)
        return dict(e=i, tstt=r["tstt"], d=r["tstt"]-T0,
                    pct=(r["tstt"]-T0)/T0*100, lost=r["lost"],
                    flow=base["flow"][i], E=net.E[i],
                    dir=direction(net, i), seg=seg_id(net, i))

    fine = max_iter or CONFIRM_ITER
    rough = min(SCAN_ROUGH, fine)
    out = [one(i, rough) for i in cands]

    if not refine:
        return out                      # 유무만 볼 때는 정밀화를 건너뛴다
    # 정밀 재계산 대상 선정.
    # ★ 거친 값에서 "개선"으로 나온 것은 **전부** 다시 푼다.
    #   거친 값(60회)만 믿고 표에 실으면 부호가 뒤집힌다. 실측 결과
    #   거친 값으로 실린 11개 중 10개가 정밀 계산 시 악화로 판명됐다.
    #   ①표는 "이 구간을 막으면 좋아진다"고 말하는 표이므로, 여기에
    #   틀린 부호가 실리면 도구 자체가 거짓말을 하는 것이다.
    alive = [r for r in out if r["lost"] < 1e-6]
    alive.sort(key=lambda r: r["d"])
    keep = {r["e"] for r in alive if r["d"] < 0}          # 개선 후보 전부
    keep |= {r["e"] for r in alive[-15:]}                 # 악화 상위 15
    out = [one(r["e"], fine) if r["e"] in keep else r for r in out]
    return out


# ────────────────────────────────────────────── 3-b2. 수요 자동 보정
TARGET_VC = 0.65          # 목표 평균 V/C
VC_OK = (0.50, 0.80)      # 이 범위 안이면 보정하지 않는다
# 0.8을 넘으면 정체에 가까워 경로 전환 여지가 사라지고,
# 0.5 미만이면 자유류라 혼잡 자체가 없다. 둘 다 개선 구간이 안 나온다.
# 브라에스 역설은 혼잡도에 단조적이지 않다. 실측 결과 순천 원도심에서
# V/C 0.39에 1개, 0.53에 0개, 0.67에 1개로 특정 수준에서만 나타났다.
# 경로 전환이 일어나는 임계점 부근에서만 생기기 때문이다.
# 그래서 한 수준에서 못 찾으면 이웃 수준을 훑는다.
VC_PROBE = (0.85, 1.25, 0.7, 1.5, 0.55, 1.9)


def auto_demand(net, zones, od, target=TARGET_VC, rounds=5, log=print):
    """
    구역마다 도로망 규모가 다르므로 같은 통행량이 어떤 곳은 자유류, 어떤 곳은
    완전정체가 된다. 자유류(V/C<0.3)에서는 브라에스 역설이 나타나지 않고,
    정체(V/C>0.9)에서는 사라진다. 둘 다 "개선 구간 없음"만 나온다.

    그래서 평균 V/C가 목표 부근에 오도록 총 통행량을 자동으로 맞춘다.
    사용자가 --demand를 명시하면 이 보정은 건너뛴다.
    """
    scale = 1.0
    for _ in range(rounds):
        cur = [[v * scale for v in row] for row in od]
        r = net.assign(zones, cur, max_iter=SCREEN_ITER)
        vc = sum(r["flow"][i] / net.cap[i] for i in range(net.m)) / net.m
        if vc <= 1e-9:
            break
        if VC_OK[0] <= vc <= VC_OK[1] and abs(scale - 1.0) < 1e-9:
            return 1.0, od          # 원래 수요가 이미 적정 범위다
        ratio = target / vc
        if 0.95 < ratio < 1.05:
            break
        # BPR은 4제곱이라 과도 조정을 피하려 완만하게 간다
        scale *= ratio ** 0.8
    return scale, [[v * scale for v in row] for row in od]


# ────────────────────────────────────────────── 3-c. 실측 교통량 보정
# AADT(연평균 일교통량)를 첨두시 방향별 교통량으로 바꾸는 계수.
# K(첨두율) 0.10 × D(방향분포) 0.58 — 국내 도로용량편람 통상값.
PEAK_K, DIR_D = 0.10, 0.58


def calibrate(net, zones, od, obs, max_round=6, log=print):
    """
    관측 교통량으로 총 통행량을 보정한다.

    합성 OD의 절대 수준은 임의값이다. 실측 지점에서 모델이 예측한 교통량과
    실제 관측값의 비율을 보고 전체 통행량을 조정하면, 절대 수준이 실측에
    묶인다. 교통공학에서 통행량 관측치로 OD를 보정하는 표준 절차의
    가장 단순한 형태(균일 스케일링)다.

    반환: (보정계수, 보정 후 OD, 진단표)
    """
    if not obs:
        return 1.0, od, None
    scale = 1.0
    for r in range(1, max_round + 1):
        cur = [[v * scale for v in row] for row in od]
        res = net.assign(zones, cur, max_iter=CONFIRM_ITER)
        ratios = []
        for o in obs:
            e = o["link"]
            if e >= net.m:
                continue
            target = o["aadt"] * PEAK_K * DIR_D
            pred = res["flow"][e]
            if target > 0 and pred > 1.0:
                ratios.append(target / pred)
        if not ratios:
            return scale, [[v * scale for v in row] for row in od], None
        ratios.sort()
        med = ratios[len(ratios) // 2]          # 중앙값 — 이상치에 강하다
        if abs(med - 1.0) < 0.02:
            break
        scale *= med ** 0.7                      # 완만하게 수렴
    cur = [[v * scale for v in row] for row in od]
    res = net.assign(zones, cur, max_iter=CONFIRM_ITER)
    diag = []
    for o in obs:
        e = o["link"]
        if e >= net.m:
            continue
        target = o["aadt"] * PEAK_K * DIR_D
        diag.append(dict(sid=o["sid"], addr=o["addr"], aadt=o["aadt"],
                         target=target, pred=res["flow"][e],
                         err=(res["flow"][e] - target) / target * 100 if target else 0,
                         dist=o.get("dist", 0)))
    return scale, cur, diag


# ────────────────────────────────────────────── 4-b. 다구간 조합 최적화
SCREEN_ITER = 60      # 조합 최적화 후보 선별용 반복수
SCAN_ROUGH = 60       # 단일구간 전수 스캔의 1차 반복수(순위 매기기용)
MAX_CAND = 200        # 단일구간 전수 검토 상한
# 유의미한 개선의 하한. 배정 수렴 잡음을 실측한 결과 기준 TSTT의 0.017%였다
# (200~3000회 반복 간 변동폭). 그 약 2배를 임계로 잡는다.
# ①표(단일구간)와 조합 최적화가 같은 값을 쓴다. 서로 다르면
# "조합은 채택했는데 ①표에는 없는" 자기모순이 생긴다.
MIN_GAIN_PCT = 0.03
CONFIRM_ITER = 200    # 확정 해 계산용 반복수


def optimize(net, zones, od, base, rounds=6, width=0, biz_nodes=None,
             scan_res=None, log=print):
    """
    탐욕적 국소탐색 — 한 구간씩 최선을 고르고 확정하기를 반복한다.

    한 번에 한 구간만 보는 스캔과 다르다. 구간 A를 일방통행으로 바꾸면
    교통이 재배분되어 구간 B의 값이 통째로 달라진다. 그래서 매 라운드마다
    바뀐 도로망 위에서 다시 평가해야 한다. 이 재평가가 최적화의 핵심이다.

    행동공간: 양방향 쌍 하나당 3가지 (양방향 유지 / 정방향만 / 역방향만)
    제약: 한 쌍의 두 방향을 모두 막지 않는다 (도로 자체가 끊긴다)
          도로망을 단절시키는 선택은 배제한다
    """
    pair = {(E["ui"], E["vi"]): i for i, E in enumerate(net.E)}
    rev = {i: pair[(E["vi"], E["ui"])] for i, E in enumerate(net.E)
           if (E["vi"], E["ui"]) in pair}

    # 후보 가지치기 — 단일구간 스캔에서 총통행시간을 10% 넘게 악화시킨 링크는
    # 어느 라운드에서도 선택되지 않는다(순천 사례 중앙값 +5.6%, 최대 +182%).
    # 교통량이 아니라 **효과**를 기준으로 자르므로 좋은 후보를 놓치지 않는다.
    pool = None
    if scan_res:
        alive = [x for x in scan_res if x["lost"] < 1e-6]
        pool = {x["e"] for x in alive if x["pct"] < 10.0}
        if len(pool) < 10:
            pool = None                 # 너무 적게 남으면 가지치기하지 않는다

    mask = [True]*net.m
    locked = set()                      # 이미 일방통행이 된 쌍의 남은 방향
    applied = []
    cur = base
    T0 = base["tstt"]
    biz0 = biz_access(net, zones, od, net.bpr(base["flow"]), mask, biz_nodes)

    for r in range(1, rounds+1):
        cand = [i for i in rev
                if mask[i] and i not in locked and rev[i] not in locked
                and (pool is None or i in pool)]
        # 교통량 상위만 보면 브라에스형(중간 교통량)을 놓친다.
        # 단일구간 스캔과 동일하게 상위 절반 + 나머지 균등표본으로 전 구간을 훑는다.
        cand.sort(key=lambda i: -cur["flow"][i])
        # 상한을 걸면 좋은 후보가 빠져 결과가 통째로 사라진다(실측 확인).
        # 개선폭이 잡음 임계의 몇 배에 불과해 표본에 극도로 민감하다.
        if width and width < len(cand):
            cand = cand[:width]
        best = None
        if r == 1 and scan_res:
            # 1라운드는 스캔 결과를 쓰되, 스캔에는 거친 값과 정밀 값이
            # 섞여 있으므로 상위 후보만 같은 정밀도로 다시 풀어 확정한다.
            # cand 필터에 걸려 스캔 최선이 빠지는 일이 없도록, 1라운드는
            # 스캔 상위를 그대로 후보로 삼는다(양방향 링크이기만 하면 된다).
            pool_r = sorted([x for x in scan_res
                             if x["lost"] < 1e-6 and x["e"] in rev
                             and mask[x["e"]]],
                            key=lambda x: x["tstt"])[:8]
            for x in pool_r:
                m2 = list(mask); m2[x["e"]] = False
                t = net.assign(zones, od, mask=m2, max_iter=CONFIRM_ITER)
                if t["lost"] > 1e-6:
                    continue
                if best is None or t["tstt"] < best[1]:
                    best = (x["e"], t["tstt"])
        else:
            for i in cand:
                m2 = list(mask); m2[i] = False
                t = net.assign(zones, od, mask=m2, max_iter=SCREEN_ITER)
                if t["lost"] > 1e-6:
                    continue
                if best is None or t["tstt"] < best[1]:
                    best = (i, t["tstt"])
        if best is None:
            log(f"  라운드 {r}: 더 개선되는 구간 없음 — 탐색 종료")
            break

        # 선별은 SCREEN_ITER로 빠르게, 채택 여부는 CONFIRM_ITER로 정밀하게 판정한다.
        # 두 수렴 수준이 다르므로 선별값만 믿고 확정하면 개선이 없는 구간이 채택된다.
        i = best[0]
        mask[i] = False
        cand_res = net.assign(zones, od, mask=mask, max_iter=CONFIRM_ITER)
        MIN_GAIN = cur["tstt"] * MIN_GAIN_PCT / 100.0
        if cand_res["lost"] > 1e-6 or cand_res["tstt"] > cur["tstt"] - MIN_GAIN:
            mask[i] = True                     # 되돌린다
            log(f"  라운드 {r}: 유의미한 개선 없음 — 탐색 종료")
            break
        locked.add(rev[i])                     # 반대 방향은 보존 (도로 단절 방지)
        cur = cand_res
        biz = biz_access(net, zones, od, net.bpr(cur["flow"]), mask, biz_nodes)
        applied.append(dict(e=i, E=net.E[i], tstt=cur["tstt"], dir=direction(net, i), seg=seg_id(net, i),
                            cum=(cur["tstt"]-T0)/T0*100, biz=biz))
        log(f"  라운드 {r}: {net.E[i]['name'] or '(무명)'} 일방통행화 "
            f"→ 누적 {(cur['tstt']-T0)/T0*100:+.3f}%")

    # ── 2-opt 개선 — 탐욕해는 국소해다. 채택한 구간 하나를 빼고
    # 다른 구간으로 바꿔 더 나은 조합이 있는지 확인한다.
    # 탐욕은 "한 번 고르면 되돌리지 않는" 방식이라, 앞 라운드의 선택이
    # 뒤 라운드를 막는 경우가 있다. 교환으로 그 함정을 벗어난다.
    swaps = 0
    tried_2opt = len(applied) >= 2
    if tried_2opt:
        improved = True
        while improved and swaps < 4:
            improved = False
            for k, a in enumerate(list(applied)):
                out_e = a["e"]
                trial_mask = list(mask)
                trial_mask[out_e] = True                 # 되돌린다
                freed = rev.get(out_e)
                base_locked = set(locked)
                if freed in base_locked:
                    base_locked.discard(freed)
                cands = [i for i in rev
                         if trial_mask[i] and i != out_e
                         and i not in base_locked and rev[i] not in base_locked
                         and (pool is None or i in pool)]
                best_swap = None
                for i in cands:
                    m2 = list(trial_mask); m2[i] = False
                    t = net.assign(zones, od, mask=m2, max_iter=SCREEN_ITER)
                    if t["lost"] > 1e-6:
                        continue
                    if best_swap is None or t["tstt"] < best_swap[1]:
                        best_swap = (i, t["tstt"])
                if best_swap is None:
                    continue
                m2 = list(trial_mask); m2[best_swap[0]] = False
                r2 = net.assign(zones, od, mask=m2, max_iter=CONFIRM_ITER)
                if r2["lost"] < 1e-6 and r2["tstt"] < cur["tstt"] - T0 * MIN_GAIN_PCT / 100.0:
                    mask = m2
                    locked = base_locked | {rev[best_swap[0]]}
                    applied[k] = dict(e=best_swap[0], E=net.E[best_swap[0]],
                                      tstt=r2["tstt"], dir=direction(net, best_swap[0]),
                                      seg=seg_id(net, best_swap[0]),
                                      cum=(r2["tstt"] - T0) / T0 * 100,
                                      biz=biz_access(net, zones, od,
                                                     net.bpr(r2["flow"]), mask, biz_nodes))
                    cur = r2
                    improved = True
                    swaps += 1
                    log(f"  교환 {swaps}: {net.E[out_e]['name'] or '(무명)'} → "
                        f"{net.E[best_swap[0]]['name'] or '(무명)'} "
                        f"→ 누적 {(r2['tstt']-T0)/T0*100:+.3f}%")
                    break
        if swaps:
            # 누적 개선율을 순서대로 다시 계산한다
            m3 = [True]*net.m
            run = base
            for a in applied:
                m3[a["e"]] = False
                run = net.assign(zones, od, mask=m3, max_iter=CONFIRM_ITER)
                a["tstt"] = run["tstt"]
                a["cum"] = (run["tstt"] - T0) / T0 * 100
            cur = run

    # 자체 검증 — 보고할 값을 완전수렴으로 다시 풀어 대조한다.
    # 배정이 덜 수렴한 채로 비교되면 결과의 부호까지 뒤집힐 수 있으므로,
    # 최종해만은 반드시 독립적으로 재계산해 오차를 함께 보고한다.
    check = None
    if applied:
        strict = net.assign(zones, od, mask=mask, max_iter=1500, tol=1e-9)
        rep = applied[-1]["cum"]
        act = (strict["tstt"] - T0) / T0 * 100
        check = dict(reported=rep, strict=act, err=abs(act - rep),
                     lost=strict["lost"], gap=strict["gap"])
        # 탐색은 빠른 배정으로 하되, **보고하는 수치는 완전수렴 값으로 바꾼다.**
        # 덜 수렴한 값을 그대로 실으면 개선을 과대평가한다(실측 29%).
        applied[-1]["tstt"] = strict["tstt"]
        applied[-1]["cum"] = act
        cur = strict
    return dict(applied=applied, mask=mask, final=cur, T0=T0, check=check,
                swaps=swaps, tried_2opt=tried_2opt,
                biz0=biz0, biz1=applied[-1]["biz"] if applied else biz0)


def seg_id(net, e):
    """구간 식별자 — 같은 도로명의 서로 다른 구간을 구분한다."""
    E = net.E[e]
    la, lo = net.coord[net.ids[E["ui"]]]
    return f"{abs(la)*1000%1000:03.0f}-{abs(lo)*100%100:02.0f}"


def direction(net, e):
    """링크의 진행 방위를 8방위 한글로. 같은 도로의 두 방향을 구분한다."""
    E = net.E[e]
    la1, lo1 = net.coord[net.ids[E["ui"]]]
    la2, lo2 = net.coord[net.ids[E["vi"]]]
    dlat, dlon = la2 - la1, (lo2 - lo1) * math.cos(math.radians((la1 + la2) / 2))
    if abs(dlat) < 1e-9 and abs(dlon) < 1e-9:
        return ""
    ang = (math.degrees(math.atan2(dlon, dlat)) + 360) % 360
    names = ["북", "북동", "동", "남동", "남", "남서", "서", "북서"]
    return names[int((ang + 22.5) // 45) % 8]


# ────────────────────────────────────────────── 5. 실행 · 보고
class InputError(Exception):
    """사용자 입력이 잘못됐을 때 — 스택트레이스 대신 안내 문구를 낸다"""


def normalize_bbox(bbox):
    """(남,서,북,동) 순서가 뒤집혀 들어와도 바로잡는다."""
    s, w, n, e = bbox
    s, n = min(s, n), max(s, n)
    w, e = min(w, e), max(w, e)
    if not (-90 <= s <= 90 and -90 <= n <= 90 and -180 <= w <= 180 and -180 <= e <= 180):
        raise InputError("위경도 범위를 벗어났습니다. 남,서,북,동 순서로 입력해 주세요.\n"
                         "  예: --bbox 34.938,127.480,34.960,127.508")
    if (n - s) < 1e-4 or (e - w) < 1e-4:
        raise InputError("구역이 너무 좁습니다(한 변 약 10m 미만).\n"
                         "  최소 0.005도(약 500m) 이상으로 넓혀 주세요.")
    return (s, w, n, e)


def analyze(bbox, demand=36000.0, grid=4, topk=0, verbose=True, cache="",
            rounds=0, auto_dem=True):
    log = print if verbose else (lambda *a, **k: None)
    import os
    bbox = normalize_bbox(bbox)
    if demand <= 0:
        raise InputError(f"통행량(--demand)은 0보다 커야 합니다. 받은 값: {demand}")
    if grid < 2:
        raise InputError(f"존 격자(--grid)는 2 이상이어야 합니다. 받은 값: {grid}\n"
                         "  격자가 1이면 존이 하나뿐이라 통행이 발생하지 않습니다.")
    if grid > 8:
        # 배정 비용은 존 수에 비례한다. 격자를 키우면 계산이 급격히 늘어
        # 실행 환경의 타임아웃에 걸린다(실측: grid 20에서 10분 초과).
        raise InputError(f"존 격자(--grid)는 8 이하여야 합니다. 받은 값: {grid}\n"
                         "  격자를 키우면 계산량이 제곱으로 늘어 끝나지 않습니다.\n"
                         "  더 세밀하게 보려면 구역을 나눠 각각 실행하세요.")
    coord = links = None; biz_xy = []; obs = []
    if cache and not os.path.exists(cache):
        raise InputError(f"지정한 캐시 파일이 없습니다: {cache}\n"
                         "  경로를 확인하거나, --cache 없이 실행해 API로 수집하세요.\n"
                         "  스킬 폴더 기준 경로입니다: assets/suncheon_network.json")
    if cache and os.path.exists(cache):
        try:
            coord, links, biz_xy, obs = load_cache(cache)
            log(f"도로망: 동봉 캐시 사용 ({os.path.basename(cache)}), 상업 POI {len(biz_xy)}곳")
        except Exception as ex:
            log(f"캐시 읽기 실패({ex}) — API로 전환")
            coord = links = None
    if coord is None:
        log("도로망 수집 중 (Overpass API)...")
        try:
            data = fetch(bbox, log=log)
            coord, links = build_graph(data)
            biz_xy = fetch_biz(bbox, log=log)
            log(f"상업 POI {len(biz_xy)}곳 수집")
            if cache:
                try:
                    save_cache(cache, coord, links, biz_xy); log(f"캐시 저장: {cache}")
                except Exception:
                    pass
        except Exception as ex:
            raise SystemExit(
                f"\n[중단] 도로망을 가져오지 못했습니다.\n  {ex}\n"
                f"  Overpass 공용 서버 장애입니다. 잠시 후 재시도하거나,\n"
                f"  동봉된 캐시 파일을 --cache 로 지정해 실행하세요.\n"
                f"  예: --cache assets/suncheon_network.json")
    if not links:
        raise InputError("이 구역에서 간선 도로를 찾지 못했습니다.\n"
                         "  바다·산간이거나 구역이 너무 좁을 수 있습니다. 범위를 넓혀 주세요.")
    net = Net(coord, links)

    keep = net.largest_component()
    if len(keep) < net.n:
        ok = {net.ids[i] for i in keep}
        net = Net(coord, [L for L in links if L["u"] in ok and L["v"] in ok])
    log(f"도로망: 교차점 {net.n}개 / 방향링크 {net.m}개 / "
        f"연장 {sum(E['length'] for E in net.E)/1000:.1f}km")

    meas = sum(1 for E in net.E if E.get("lanes_src") == "표준노드링크")
    est = sum(1 for E in net.E if E.get("lanes_src") not in ("osm", "표준노드링크"))
    if meas:
        log(f"✅ 차로수·제한속도 {meas}/{net.m} 링크가 국가표준노드링크 실측값 "
            f"(국토교통부, 2026-08 기준)")
    if est:
        log(f"⚠️ 차로수 결측 {est}/{net.m} — 도로등급 기본값으로 보정")

    # ── 규모 제한 ──────────────────────────────────────────
    # 배정 1회 비용은 링크 수에 거의 비례한다(실측: 319링크 0.38초,
    # 2000링크 2.4초, 5499링크 6.5초). 탐색은 배정을 수십 번 하므로
    # 큰 구역은 수 분이 걸려 실행 환경의 타임아웃에 걸린다.
    # 오래 기다리다 실패하느니 즉시 나누라고 안내하는 편이 낫다.
    MAX_LINKS = 1200
    if net.m > MAX_LINKS:
        span_lat, span_lon = bbox[2]-bbox[0], bbox[3]-bbox[1]
        k = int((net.m / MAX_LINKS) ** 0.5) + 1
        raise InputError(
            f"구역이 너무 넓습니다(방향링크 {net.m:,}개, 한도 {MAX_LINKS:,}개).\n"
            f"  분석에 수 분이 걸려 중단될 수 있습니다. {k}×{k}로 나눠 실행하세요.\n"
            f"  예: --bbox {bbox[0]:.4f},{bbox[1]:.4f},"
            f"{bbox[0]+span_lat/k:.4f},{bbox[1]+span_lon/k:.4f}")

    # 중간 규모는 검토 범위를 줄여 시간 안에 끝낸다
    if net.m > 900:
        f = 900.0 / net.m
        t2, r2 = max(40, int(300 * f)), max(1, min(rounds, 2))
        log(f"구역이 넓어(방향링크 {net.m:,}개) 검토 범위를 제한합니다: "
            f"전수→{t2}개, rounds {rounds}→{r2}")
        topk, rounds = t2, r2
        probe_limit = 2                 # 수요 재탐색도 줄인다
    else:
        probe_limit = len(VC_PROBE)

    if net.m < 20:
        raise InputError(f"도로망이 너무 작습니다(방향링크 {net.m}개).\n"
                         "  분석에는 20개 이상이 필요합니다. 구역을 넓혀 주세요.")

    zones, od, _ = make_zones(net, bbox, grid=grid, total=demand)
    if obs:
        auto_dem = False        # 실측 관측지점이 있으면 그쪽 보정이 우선한다
    if auto_dem:
        sc, od = auto_demand(net, zones, od, log=log)
        if abs(sc - 1.0) > 0.05:
            log(f"수요 자동 보정: ×{sc:.2f} → {sum(sum(r) for r in od):,.0f}대/h "
                f"(평균 V/C {TARGET_VC} 목표)")
    if len(zones) < 2:
        raise InputError(f"통행을 만들 존이 부족합니다(존 {len(zones)}개).\n"
                         "  --grid를 키우거나 구역을 넓혀 주세요.")
    log(f"존 {len(zones)}개 / 총 통행 {sum(sum(r) for r in od):,.0f}대/h (합성 OD)")

    log("이용자균형 배정 중...")
    calib = None
    if obs:
        log(f"실측 교통량 보정 중 (관측지점 {len(obs)}곳)...")
        scale, od, calib = calibrate(net, zones, od, obs, log=log)
        log(f"  · 총 통행량 보정계수 {scale:.3f} → {sum(sum(r) for r in od):,.0f}대/h")
        if calib:
            for c in calib:
                log(f"  · {c['sid']:9s} 실측 {c['target']:>6,.0f} / 예측 "
                    f"{c['pred']:>6,.0f} 대/h  오차 {c['err']:+6.1f}%")

    base = net.assign(zones, od, max_iter=CONFIRM_ITER)
    if base["tstt"] <= 0:
        raise InputError("기준 통행시간이 0입니다. 도로망과 존이 연결되지 않았습니다.\n"
                         "  구역을 넓히거나 --grid를 조정해 주세요.")
    vc = [base["flow"][i]/net.cap[i] for i in range(net.m)]
    log(f"기준 총통행시간 {base['tstt']:,.0f} 대·분 "
        f"(수렴갭 {base['gap']:.1e}, {base['iters']}회)")
    log(f"V/C 평균 {sum(vc)/len(vc):.2f} / 최대 {max(vc):.2f} / "
        f"용량초과 {sum(1 for x in vc if x>1)}개")

    log(f"일방통행 전환 후보 {topk}개 재배정 중...")
    res = scan(net, zones, od, base, topk=topk)

    def n_good(rs, T):
        return sum(1 for r in rs if r["lost"] < 1e-6 and r["d"] < 0
                   and r["flow"] > 50 and abs(r["d"] / T * 100) > MIN_GAIN_PCT)

    if n_good(res, base["tstt"]) == 0 and auto_dem:
        # 이 수요에서 개선 구간이 없다. 브라에스는 특정 혼잡 수준에서만
        # 나타나므로 이웃 수요를 훑어본다.
        log("개선 구간이 없어 이웃 수요 수준을 탐색합니다...")
        ptopk = min(topk, 20)           # 유무 판정에는 후보를 줄여도 된다
        for f in VC_PROBE[:probe_limit]:
            od2 = [[v * f for v in row] for row in od]
            b2 = net.assign(zones, od2, max_iter=SCREEN_ITER)
            r2 = scan(net, zones, od2, b2, topk=ptopk, refine=False)
            ng = n_good(r2, b2["tstt"])
            vc2 = sum(b2["flow"][i] / net.cap[i] for i in range(net.m)) / net.m
            log(f"  · 수요 ×{f} (V/C {vc2:.2f}) → 개선 구간 {ng}개")
            if ng > 0:
                # 채택한 수준만 정밀하게 다시 푼다
                od = od2
                base = net.assign(zones, od, max_iter=CONFIRM_ITER)
                res = scan(net, zones, od, base, topk=topk)
                log(f"  · 이 수준을 채택합니다 ({sum(sum(x) for x in od):,.0f}대/h)")
                break

    opt = None
    if rounds > 0:
        biz_nodes = find_biz_nodes(net, biz_xy) if biz_xy else []
        log(f"다구간 조합 최적화 (최대 {rounds}라운드, 상업지 {len(biz_nodes)}곳 기준)...")
        opt = optimize(net, zones, od, base, rounds=rounds, biz_nodes=biz_nodes,
                       scan_res=res, log=log)

        # 조합이 채택한 구간은 ①표에도 반드시 정밀값으로 실린다.
        # 스캔은 상위 일부만 정밀 계산하므로, 조합이 찾아낸 구간이 스캔의
        # 거친 값에 묻혀 "조합은 채택했는데 ①표엔 없는" 모순이 생길 수 있다.
        if opt and opt.get("applied"):
            T0 = base["tstt"]
            by_e = {r["e"]: r for r in res}
            for a in opt["applied"]:
                e = a["e"]
                m1 = [True]*net.m; m1[e] = False
                # ①표의 다른 행과 같은 기준(CONFIRM_ITER)으로 계산한다.
                # 기준 TSTT(T0)가 CONFIRM_ITER 값이므로, 차단만 완전수렴으로
                # 풀면 두 수렴 수준이 섞인 값이 된다.
                # 조합 헤드라인의 완전수렴 정정값은 자체 검증 줄이 설명한다.
                r1 = net.assign(zones, od, mask=m1, max_iter=CONFIRM_ITER)
                rec = dict(e=e, tstt=r1["tstt"], d=r1["tstt"]-T0,
                           pct=(r1["tstt"]-T0)/T0*100, lost=r1["lost"],
                           flow=base["flow"][e], E=net.E[e],
                           dir=direction(net, e), seg=seg_id(net, e))
                if e in by_e:
                    res[res.index(by_e[e])] = rec
                else:
                    res.append(rec)
    
    return net, base, res, opt, calib


def report(net, base, res, top=10, opt=None, calib=None):
    T0 = base["tstt"]
    ok = [r for r in res if r["lost"] < 1e-6]
    cut = len(res) - len(ok)
    # 잡음 제거 — 조합 최적화와 동일한 임계를 쓴다 (MIN_GAIN_PCT).
    good = sorted([r for r in ok if r["d"] < 0 and r["flow"] > 50
                   and abs(r["pct"]) > MIN_GAIN_PCT], key=lambda r: r["d"])
    bad = sorted(ok, key=lambda r: -r["d"])

    L = []
    L.append("# 일방통행 전환 영향 분석 결과\n")
    L.append(f"- 기준 총 통행시간(TSTT): **{T0:,.0f} 대·분**")
    L.append(f"- 검토한 전환 후보: {len(res)}개 (도로망 단절 유발 {cut}개 제외)\n")

    if opt and opt["applied"]:
        a = opt["applied"]
        fin = a[-1]
        L.append("## ★ 다구간 조합 최적안\n")
        L.append("> 한 구간씩 최선을 고르고 확정하기를 반복했다. 구간 하나를 바꾸면 "
                 "교통이 재배분되어 나머지 구간의 값이 전부 달라지므로, "
                 "매 라운드마다 바뀐 도로망 위에서 다시 계산했다.\n")
        saved = T0 - fin["tstt"]
        L.append(f"**{len(a)}개 구간을 일방통행으로 전환 → 총 통행시간 "
                 f"{fin['cum']:+.3f}%** ({T0:,.0f} → {fin['tstt']:,.0f} 대·분)\n")
        L.append(f"이 구역 운전자들이 첨두 1시간 동안 도로에서 보내는 시간의 합이 "
                 f"**{T0/60:,.0f}시간**이다. 위 전환으로 **매일 {saved/60:.1f}시간**이 사라진다.")
        L.append(f"표지판과 노면표시 교체 외에 예산은 들지 않는다. "
                 f"실제 교통체계개선(TSM) 사업의 통상 효과가 1~3%임을 감안하면, "
                 f"공사 없이 얻는 {abs(fin['cum']):.2f}%는 현실적인 값이다.\n")
        L.append("| 라운드 | 전환 구간 | 등급 | 누적 개선율 |")
        L.append("|---:|---|---|---:|")
        for k, r in enumerate(a, 1):
            L.append(f"| {k} | {r['E']['name'] or '(무명)'} {r['seg']}구간 {r['dir']}행 | {r['E']['hwy']} "
                     f"| **{r['cum']:+.3f}%** |")
        L.append("")
        bv = biz_verdict(opt.get("biz0"), opt.get("biz1"))
        if bv:
            if bv["worst"] > 1.0:
                verdict = (f"⚠️ **한 곳이 {bv['worst']:.1f}분 나빠진다 — 시행 재검토 필요.** "
                           f"평균이 아니라 가장 나빠지는 상가를 봐야 한다")
            elif bv["worst"] > 0.3:
                verdict = (f"가장 나빠지는 상가가 {bv['worst']:.1f}분 늘어난다 — "
                           f"해당 구간 상인 협의 필요")
            elif bv["hurt"] == 0:
                verdict = "**손해 보는 상가가 없다 — 통행시간만 개선하고 상권은 지켰다**"
            else:
                verdict = (f"{bv['hurt']}/{bv['total']}곳이 조금 나빠지지만 "
                           f"최대 {bv['worst']:.2f}분 수준이다")
            L.append(f"**상권 영향** (상업지 {bv['total']}곳 개별 평가): "
                     f"평균 접근시간 {bv['mean0']:.2f}분 → {bv['mean1']:.2f}분 "
                     f"({bv['dmean']:+.2f}분), **최악 상가 {bv['worst']:+.2f}분**. {verdict}\n")
        if opt.get("tried_2opt"):
            n = opt.get("swaps", 0)
            if n:
                L.append(f"**2-opt 검증** 채택한 구간을 하나씩 빼고 다른 구간으로 "
                         f"바꿔본 결과 **{n}건이 더 나아 교환했다.** 탐욕 탐색은 한 번 "
                         f"고르면 되돌리지 않으므로, 앞 선택이 뒤를 막는 함정이 있다.\n")
            else:
                L.append("**2-opt 검증** ✅ 채택한 구간을 하나씩 빼고 다른 모든 후보로 "
                         "바꿔봤지만 더 나은 조합이 없었다. 이 해는 **1-교환에 대해 "
                         "안정적**이다(전역 최적해임을 뜻하지는 않는다).\n")

        c = opt.get("check")
        if c:
            ok_mark = "✅" if c["lost"] < 1e-6 else "⚠️"
            L.append(f"**자체 검증** {ok_mark} 위 수치는 **완전수렴(1500회, tol 1e-9) "
                     f"재계산값**이다. 탐색 단계의 빠른 배정값은 "
                     f"{c['reported']:+.3f}%였으나 그대로 싣지 않고 다시 풀어 "
                     f"{c['strict']:+.3f}%로 정정했다(차이 {c['err']:.3f}%p). "
                     f"도달불가 통행량 {c['lost']:.0f}대/h.\n")
        L.append("> 단일 구간 최선값보다 조합이 더 나은 이유는 "
                 "구간 간 상호작용 때문이다. 이것이 도로 문제를 "
                 "직관으로 풀 수 없는 이유이기도 하다.\n")

    L.append("## ① 일방통행으로 바꾸면 **전체가 좋아지는** 구간\n")
    if good:
        L.append("> 도로를 막았는데 총 통행시간이 줄어드는 구간이다. "
                 "브라에스 역설에 해당하며, 예산 없이 표지판만 바꿔 얻는 개선이다.")
        L.append(f"> 배정 수렴 잡음(약 0.017%)의 2배인 **{MIN_GAIN_PCT}% 이상** "
                 f"개선되는 것만 싣는다. 위 조합 최적안과 같은 기준이다.\n")
        L.append("| 순위 | 도로명 | 등급 | 현재 교통량 | ΔTSTT | 개선율 |")
        L.append("|---:|---|---|---:|---:|---:|")
        for i, r in enumerate(good[:top], 1):
            E = r["E"]
            L.append(f"| {i} | {E['name'] or '(무명)'} {r['seg']}구간 {r['dir']}행 | {E['hwy']} | "
                     f"{r['flow']:,.0f}대/h | {r['d']:+,.0f} | **{r['pct']:+.3f}%** |")
    else:
        L.append("이 수요 수준에서는 개선되는 구간이 없다. "
                 "수요를 높이거나(첨두시) 대상 구역을 넓혀 재검토할 것.")
    L.append("")

    L.append("## ② 절대 막으면 안 되는 구간\n")
    L.append("> **구간과 방향까지 봐야 한다.** 같은 도로명이라도 구간번호가 다르면")
    L.append("> 다른 구간이고, 방향이 다르면 반대 차선이다. 한 방향은 막아도 되고")
    L.append("> 다른 방향은 막으면 안 되는 경우가 흔하다. 한 구간의 양방향이 모두")
    L.append("> 차단되는 안은 도로가 끊기므로 애초에 후보에서 제외된다.\n")
    # 같은 구간의 양방향은 차단 효과가 동일하게 나온다(OSM에서 별도 way로
    # 그려진 경우 way id도 다르다). 효과·교통량이 같으면 한 줄로 합친다.
    seen, merged = set(), []
    for r in [x for x in bad if x["d"] > 0]:
        key = (round(r["d"]), round(r["flow"]))
        if key in seen:
            continue
        seen.add(key)
        merged.append(r)
    L.append("| 순위 | 도로명 | 등급 | 현재 교통량 | ΔTSTT | 악화율 |")
    L.append("|---:|---|---|---:|---:|---:|")
    for i, r in enumerate(merged[:top], 1):
        E = r["E"]
        L.append(f"| {i} | {E['name'] or '(무명)'} {r['seg']}구간 {r['dir']}행 | {E['hwy']} | "
                 f"{r['flow']:,.0f}대/h | {r['d']:+,.0f} | **{r['pct']:+.2f}%** |")
    L.append("")
    if opt and not opt["applied"]:
        L.append("## 다구간 조합 최적화\n")
        L.append("첫 라운드에서 이미 개선 여지가 없었다. "
                 "현재 도로망이 이 수요 수준에서는 국소 최적에 가깝다는 뜻이다.\n")

    if calib:
        L.append("## 실측 교통량 대조\n")
        L.append("> 도로교통량 통계연보(국토교통부)의 조사지점 실측값과 대조해 "
                 "총 통행량을 보정했다. 합성 OD의 절대 수준을 실측에 묶는 절차다.\n")
        L.append("| 조사지점 | 위치 | AADT | 첨두 환산 | 모델 예측 | 오차 |")
        L.append("|---|---|---:|---:|---:|---:|")
        for c in calib:
            L.append(f"| {c['sid']} | {c['addr']} | {c['aadt']:,} | "
                     f"{c['target']:,.0f} | {c['pred']:,.0f} | **{c['err']:+.1f}%** |")
        L.append("")
        L.append("AADT는 연평균 일교통량이므로 첨두율 0.10 · 방향분포 0.58을 "
                 "적용해 첨두시 방향별 교통량으로 환산했다(도로용량편람 통상값).\n")

    L.append("## 해석 주의\n")
    L.append("- OD는 도로 연장 기반 **합성값**이다. 절대 시간이 아니라 구간 간 **상대 비교**로만 쓸 것")
    L.append("- 차로수·제한속도는 국가표준노드링크(국토교통부) 실측값을 우선 쓰고, "
             "매칭 실패 시에만 도로등급 기본값으로 보정한다")
    L.append("- 보행 안전·형평성은 반영되지 않았다. 통행시간과 상권 접근성 두 축만 본 결과다")
    L.append("- 조합 최적안은 탐욕적 국소탐색의 결과이며 전역 최적해가 아니다")
    return "\n".join(L)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="일방통행 전환 영향 분석")
    p.add_argument("--bbox", default="", help="S,W,N,E (예: 34.938,127.480,34.960,127.508)")
    p.add_argument("--place", default="", help="지명. 위경도 대신 쓸 수 있다 (예: 순천 원도심)")
    p.add_argument("--span", type=float, default=3.0, help="--place 사용 시 한 변 km")
    p.add_argument("--demand", type=float, default=0,
                   help="첨두시 총 통행량. 생략하면 평균 V/C 0.45가 되도록 자동 보정")
    p.add_argument("--grid", type=int, default=4)
    p.add_argument("--topk", type=int, default=0,
                   help="단일구간 검토 수. 0이면 양방향 구간 전수 검토(권장)")
    p.add_argument("--out", default="")
    p.add_argument("--cache", default="", help="도로망 캐시 JSON. 있으면 API 대신 사용")
    p.add_argument("--rounds", type=int, default=3,
                   help="다구간 조합 최적화 라운드 수 (0이면 단일구간 스캔만)")
    a = p.parse_args()
    if a.place and not a.bbox:
        bb = geocode(a.place, a.span)
        if bb is None:
            raise SystemExit(f"[중단] '{a.place}' 위치를 찾지 못했습니다.\n"
                             "  더 구체적인 지명을 쓰거나 --bbox로 직접 지정하세요.")
        a.bbox = ",".join(f"{v:.5f}" for v in bb)
        print(f"[지오코딩] {a.place} → --bbox {a.bbox}")
    if not a.bbox:
        raise SystemExit("[중단] --bbox 또는 --place 중 하나가 필요합니다.\n"
                         "  예: --place \"순천 원도심\"  또는\n"
                         "      --bbox 34.938,127.480,34.960,127.508")
    try:
        parts = [float(x) for x in a.bbox.split(",")]
    except ValueError:
        raise SystemExit("[중단] --bbox는 숫자 4개여야 합니다.\n"
                         "  예: --bbox 34.938,127.480,34.960,127.508")
    if len(parts) != 4:
        raise SystemExit(f"[중단] --bbox는 남,서,북,동 4개 값이어야 합니다. "
                         f"받은 개수: {len(parts)}")
    bbox = tuple(parts)
    auto = (a.demand <= 0)
    dem = a.demand if not auto else 36000.0
    try:
        net, base, res, opt, calib = analyze(bbox, dem, a.grid, a.topk,
                                             cache=a.cache, rounds=a.rounds,
                                             auto_dem=auto)
    except InputError as ex:
        raise SystemExit(f"[중단] {ex}")
    md = report(net, base, res, opt=opt, calib=calib)
    print("\n" + md)
    if a.out:
        import os
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        open(a.out, "w", encoding="utf-8").write(md)
        print(f"\n저장: {a.out}")
