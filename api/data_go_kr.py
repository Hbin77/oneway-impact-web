"""
전국일방통행도로표준데이터(공공데이터포털) 조회 + 시도/시군구 24시간 캐시.

규칙:
- 키는 DATA_GO_KR_KEY(환경변수)로만 읽는다. 코드·로그·응답·프론트에 키 값을 쓰지 않는다.
- 엔진 계산에는 쓰지 않는다. 분석 결과 소비 레이어(Solar 프롬프트, 프론트 표시)에서만 쓴다.
- 같은 시군구는 24시간 캐시해 반복 호출을 막는다.
- API 실패·키 없음·0건이면 빈 목록으로 두고 서비스는 정상 동작한다.
- Solar/프론트에 넣는 건 요약(건수, 폭·연장 범위, 지정연도 범위, 도로명 일부)만. 원문 전체 미포함.
- 참조일은 오늘 날짜가 아니라 API 행의 referenceDate(최댓값)를 쓴다.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen, Request

API_URL = "https://api.data.go.kr/openapi/tn_pubr_public_one_way_street_api"

DATA_GO_KR_KEY = os.environ.get("DATA_GO_KR_KEY", "")

_CACHE_DIR = Path("/tmp/oneway_oneway_designations_cache")
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

_CACHE_TTL_SEC = 24 * 60 * 60


def _cache_path(sido: str, sigungu: str) -> Path:
    safe_sido = (sido or "").strip().replace("/", "_").replace("\\", "_") or "unknown"
    safe_sigungu = (sigungu or "").strip().replace("/", "_").replace("\\", "_") or "unknown"
    return _CACHE_DIR / f"{safe_sido}__{safe_sigungu}.json"


def _now_utc_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


def _load_cache(sido: str, sigungu: str) -> dict | None:
    p = _cache_path(sido, sigungu)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    updated_at = data.get("updated_at", 0)
    if _now_utc_epoch() - updated_at > _CACHE_TTL_SEC:
        return None
    return data


def _save_cache(sido: str, sigungu: str, payload: dict) -> None:
    p = _cache_path(sido, sigungu)
    out = {"updated_at": _now_utc_epoch(), "sido": sido, "sigungu": sigungu, "payload": payload}
    p.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")


def _fetch_raw(sido: str, sigungu: str) -> list[dict] | None:
    """원본 API 응답 items(item) 리스트. 실패·키 없음이면 None."""
    if not DATA_GO_KR_KEY:
        return None
    rows: list[dict] = []
    page = 1
    total = None
    while True:
        params = {
            "serviceKey": DATA_GO_KR_KEY,
            "pageNo": str(page),
            "numOfRows": "1000",
            "type": "json",
            "ctprvnNm": sido,
            "signguNm": sigungu,
        }
        url = API_URL + "?" + urlencode(params)
        try:
            req = Request(url, headers={"User-Agent": "oneway-impact-web/1.0"})
            with urlopen(req, timeout=30) as r:
                body = json.loads(r.read().decode("utf-8"))
        except Exception:
            return None

        header = body.get("header", {})
        code = header.get("resultCode")
        if code != "00":
            return None

        body_data = body.get("body", {})
        total = body_data.get("totalCount")
        try:
            total = int(total) if total is not None else 0
        except (TypeError, ValueError):
            total = 0

        items = body_data.get("items", {})
        page_items = items.get("item", [])
        if isinstance(page_items, dict):
            page_items = [page_items]
        if not isinstance(page_items, list):
            page_items = []
        rows.extend(page_items)

        if total is not None and len(rows) >= total:
            break
        if len(page_items) < 1000:
            break
        page += 1
        if page > 10:
            break
    return rows if rows else None


def _point_in_bbox(lat: float, lng: float, bbox: tuple[float, float, float, float]) -> bool:
    s, w, n, e = bbox
    return s <= lat <= n and w <= lng <= e


def _parse_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _filter_by_bbox(items: list[dict], bbox: tuple[float, float, float, float]) -> list[dict]:
    out = []
    for it in items:
        slat = _parse_float(it.get("startLatitude"))
        slng = _parse_float(it.get("startLongitude"))
        if slat is None or slng is None:
            continue
        if _point_in_bbox(slat, slng, bbox):
            out.append(it)
    return out


def _coerce_int(v) -> int | None:
    f = _parse_float(v)
    if f is None:
        return None
    return int(f)


def _item_row(it: dict) -> dict:
    """접이식 목록용 행 하나."""
    return {
        "roadNm": (it.get("roadNm") or "").strip(),
        "appnResn": (it.get("appnResn") or "").strip(),
        "appnYear": it.get("appnYear"),
        "roadBt": it.get("roadBt"),
        "roadEt": it.get("roadEt"),
        "cartrkCo": it.get("cartrkCo"),
        "mdstrpYn": it.get("mdstrpYn"),
        "startLatitude": it.get("startLatitude"),
        "startLongitude": it.get("startLongitude"),
        "endLatitude": it.get("endLatitude"),
        "endLongitude": it.get("endLongitude"),
    }


def _parse_reference_date(v) -> datetime | None:
    """referenceDate를 파싱해 date 반환. 실패 시 None."""
    if not v:
        return None
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _summary(items: list[dict]) -> dict:
    count = len(items)
    if count == 0:
        return {
            "count": 0,
            "roadBt_min": None,
            "roadBt_max": None,
            "roadEt_min": None,
            "roadEt_max": None,
            "appnYear_min": None,
            "appnYear_max": None,
            "sample": [],
            "items": [],
            "reference_date": None,
        }

    road_bt = [v for v in (_coerce_int(it.get("roadBt")) for it in items) if v is not None]
    road_et = [v for v in (_coerce_int(it.get("roadEt")) for it in items) if v is not None]
    appn_year = [v for v in (_coerce_int(it.get("appnYear")) for it in items) if v is not None]

    ref_dates = []
    for it in items:
        d = _parse_reference_date(it.get("referenceDate"))
        if d is not None:
            ref_dates.append(d)

    sample = [_item_row(it) for it in items[:5]]
    items_out = [_item_row(it) for it in items]

    ref_date = max(ref_dates).isoformat() if ref_dates else None

    return {
        "count": count,
        "roadBt_min": min(road_bt) if road_bt else None,
        "roadBt_max": max(road_bt) if road_bt else None,
        "roadEt_min": min(road_et) if road_et else None,
        "roadEt_max": max(road_et) if road_et else None,
        "appnYear_min": min(appn_year) if appn_year else None,
        "appnYear_max": max(appn_year) if appn_year else None,
        "sample": sample,
        "items": items_out,
        "reference_date": ref_date,
    }


def get_one_way_designations(
    sido: str, sigungu: str, bbox: tuple[float, float, float, float]
) -> dict:
    """
    시도/시군구 + bbox로 기존 공식 일방통행 지정 현황을 조회한다.

    반환: {"count": int, "roadBt_min/max": int|None, "roadEt_min/max": int|None,
            "appnYear_min/max": int|None, "sample": [원항목 5개 이하],
            "items": [전체 항목(roadNm 등, 프론트 접이식 목록용)],
            "reference_date": str|None, "source": str}
    API/키/캐시 실패 시 count=0, 나머지는 None/[], source는 기본 문자열.
    """
    sido = (sido or "").strip()
    sigungu = (sigungu or "").strip()
    if not sido or not sigungu or not bbox:
        return {
            "count": 0,
            "roadBt_min": None,
            "roadBt_max": None,
            "roadEt_min": None,
            "roadEt_max": None,
            "appnYear_min": None,
            "appnYear_max": None,
            "sample": [],
            "items": [],
            "reference_date": None,
            "source": "경찰청 전국일방통행도로표준데이터(공공데이터포털)",
        }

    cached = _load_cache(sido, sigungu)
    items: list[dict] | None = None
    if cached is not None:
        items = cached.get("payload", {}).get("items")
        if not isinstance(items, list):
            items = None

    if items is None:
        raw = _fetch_raw(sido, sigungu)
        if raw is None:
            return {
                "count": 0,
                "roadBt_min": None,
                "roadBt_max": None,
                "roadEt_min": None,
                "roadEt_max": None,
                "appnYear_min": None,
                "appnYear_max": None,
                "sample": [],
                "items": [],
                "reference_date": None,
                "source": "경찰청 전국일방통행도로표준데이터(공공데이터포털)",
            }
        items = _filter_by_bbox(raw, bbox)
        _save_cache(sido, sigungu, {"items": items, "bbox": list(bbox)})

    summary = _summary(items)
    ref_date = summary.get("reference_date")
    if ref_date:
        summary["source"] = (
            f"경찰청 전국일방통행도로표준데이터(공공데이터포털, 참조일 {ref_date})"
        )
    else:
        summary["source"] = "경찰청 전국일방통행도로표준데이터(공공데이터포털)"
    return summary
