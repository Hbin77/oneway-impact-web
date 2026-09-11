import json
import os
import subprocess
import sys
from urllib.request import Request, urlopen
from urllib.parse import urlencode

from flask import Flask, request, jsonify

app = Flask(__name__)

GOOGLE_KEY = os.environ.get("GOOGLE_MAPS_GEOCODING_API_KEY", "")

API_BASE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(API_BASE, "road_impact.py")

SUNCHOON_BBOX = (34.938, 127.480, 34.960, 127.508)


def _bboxes_overlap(a, b):
    """두 bbox(남,서,북,동)가 겹치는지 확인."""
    return (
        max(a[0], b[0]) < min(a[2], b[2])
        and max(a[1], b[1]) < min(a[3], b[3])
    )



def _google_geocode(place):
    if not GOOGLE_KEY:
        return None
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    req = Request(
        url,
        data=urlencode(
            {
                "address": place,
                "key": GOOGLE_KEY,
                "region": "kr",
                "language": "ko",
            }
        ).encode(),
    )
    try:
        with urlopen(req, timeout=20) as r:
            if r.status != 200:
                return None
            data = json.loads(r.read().decode())
    except Exception:
        return None
    results = data.get("results", [])
    if not results:
        return None
    loc = results[0].get("geometry", {}).get("location", {})
    lat = float(loc.get("lat", 0))
    lng = float(loc.get("lng", 0))
    return (lat, lng)


def geocode(place):
    """지명 -> (위도, 경도). Google Maps Geocoding API 사용."""
    if not place:
        return None
    return _google_geocode(place)


def _validate_bbox(bbox):
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise ValueError("bbox는 숫자 4개여야 합니다.")
    out = []
    for v in bbox:
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError("bbox 값은 숫자여야 합니다.")
        out.append(f)
    return tuple(out)


def run_analysis(bbox, use_cache=False):
    bbox = _validate_bbox(bbox)
    args = [
        sys.executable,
        SCRIPT,
        "--bbox",
        ",".join(str(v) for v in bbox),
        "--rounds",
        "3",
    ]
    if use_cache:
        args += ["--cache", os.path.join(API_BASE, "suncheon_network.json")]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        return None, "분석 시간이 너무 오래 걸려 중단됐습니다."
    except Exception:
        return None, "분석 실행 중 오류가 발생했습니다."
    if proc.returncode != 0:
        return None, "분석 엔진 실행이 실패했습니다."
    # road_impact.py 는 마크다운만 출력한다. stdout 을 그대로 반환.
    return proc.stdout, None


@app.route("/api/analyze", methods=["POST"])
def analyze():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return (
            jsonify(
                {
                    "error": "요청 형식이 잘못됐습니다. JSON 객체를 보내 주세요.",
                    "status": 400,
                }
            ),
            400,
            {"Content-Type": "application/json; charset=utf-8"},
        )
    place = (body.get("place") or "").strip()
    if not place:
        return (
            jsonify(
                {
                    "error": "지명이 비어 있습니다. 지명 텍스트를 하나 입력해 주세요.",
                    "status": 400,
                }
            ),
            400,
            {"Content-Type": "application/json; charset=utf-8"},
        )

    loc = geocode(place)
    if loc is None:
        return (
            jsonify(
                {
                    "error": f"'{place}' 위치를 찾지 못했습니다. 더 구체적인 지명이나 주소를 입력해 주세요.",
                    "status": 404,
                }
            ),
            404,
            {"Content-Type": "application/json; charset=utf-8"},
        )

    lat, lng = loc
    bbox = (lat - 0.015, lng - 0.015, lat + 0.015, lng + 0.015)

    # 순천 캐시 bbox(34.938,127.480,34.960,127.508)와 겹칠 때만 --cache 사용
    use_cache = _bboxes_overlap(bbox, SUNCHOON_BBOX)
    result_text, err = run_analysis(bbox, use_cache=use_cache)
    if err:
        return (
            jsonify(
                {"error": "분석 중 오류가 발생했습니다.", "detail": err, "status": 500}
            ),
            500,
            {"Content-Type": "application/json; charset=utf-8"},
        )

    return (
        jsonify(
            {
                "status": 200,
                "place": place,
                "bbox": list(bbox),
                "markdown": result_text or "",
            }
        ),
        200,
        {"Content-Type": "application/json; charset=utf-8"},
    )


@app.route("/api/analyze", methods=["GET"])
def analyze_get():
    """GET 요청은 지원하지 않는다."""
    return (
        jsonify(
            {
                "error": "POST 요청만 지원합니다. JSON 본문에 place를 포함해 주세요.",
                "status": 405,
            }
        ),
        405,
        {"Content-Type": "application/json; charset=utf-8"},
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=True)
