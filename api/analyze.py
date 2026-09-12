import json
import os
import subprocess
import sys
import time
import uuid
from urllib.request import urlopen
from urllib.parse import urlencode
from pathlib import Path

from flask import Flask, request, jsonify

app = Flask(__name__)

GOOGLE_KEY = os.environ.get("GOOGLE_MAPS_GEOCODING_API_KEY", "")

API_BASE = Path(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = API_BASE / "road_impact.py"

SUNCHOON_BBOX = (34.938, 127.480, 34.960, 127.508)

JOBS_DIR = Path("/tmp/oneway_jobs")
JOBS_DIR.mkdir(parents=True, exist_ok=True)


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
    params = urlencode(
        {
            "address": place,
            "key": GOOGLE_KEY,
            "region": "kr",
            "language": "ko",
        }
    )
    full_url = url + "?" + params
    try:
        with urlopen(full_url, timeout=20) as r:
            if r.status != 200:
                return None
            data = json.loads(r.read().decode())
    except Exception:
        return None
    status = data.get("status")
    if status != "OK":
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


def _rev_geocode(lat, lng):
    """위경도 -> 주소 문자열. Google Geocoding reverse 사용."""
    if not GOOGLE_KEY:
        return None
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = urlencode(
        {
            "latlng": f"{lat},{lng}",
            "key": GOOGLE_KEY,
            "language": "ko",
        }
    )
    full_url = url + "?" + params
    try:
        with urlopen(full_url, timeout=20) as r:
            if r.status != 200:
                return None
            data = json.loads(r.read().decode())
    except Exception:
        return None
    status = data.get("status")
    if status != "OK":
        return None
    results = data.get("results", [])
    if not results:
        return None
    return (
        results[0].get("formatted_address")
        or results[0].get("address_components", [{}])[0].get("long_name", "")
    )


def _job_path(job_id):
    return JOBS_DIR / f"{job_id}.json"


def _save_job(job_id, state):
    _job_path(job_id).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def _load_job(job_id):
    p = _job_path(job_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _start_job(job_id, args, use_cache):
    """엔진을 백그라운드 프로세스로 시작한다."""
    full_args = [
        sys.executable,
        str(SCRIPT),
        "--bbox",
        ",".join(str(v) for v in args["bbox"]),
        "--rounds",
        "3",
    ]
    if use_cache:
        full_args += ["--cache", str(API_BASE / "suncheon_network.json")]

    try:
        proc = subprocess.Popen(
            full_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as exc:
        _save_job(job_id, {
            "status": "error",
            "error": f"분석 프로세스 실행 실패: {exc}",
        })
        return

    _save_job(job_id, {
        "status": "running",
        "pid": proc.pid,
        "args": full_args,
        "started_at": time.time(),
    })

    # 백그라운드에서 완료될 때까지 대기 후 결과 저장
    def _wait_and_save():
        try:
            stdout, stderr = proc.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            _save_job(job_id, {
                "status": "error",
                "error": "분석 시간이 너무 오래 걸려 중단됐습니다.",
            })
            return
        except Exception:
            proc.kill()
            stdout, stderr = proc.communicate()
            _save_job(job_id, {
                "status": "error",
                "error": "분석 실행 중 오류가 발생했습니다.",
            })
            return

        if proc.returncode != 0:
            _save_job(job_id, {
                "status": "error",
                "error": "분석 엔진 실행이 실패했습니다.",
            })
            return

        _save_job(job_id, {
            "status": "done",
            "markdown": stdout or "",
            "bbox": args["bbox"],
            "place": args["place"],
        })

    # 별도 스레드 없이 단순 호출은 블로킹되므로, 별도 프로세스로 대기
    # Popen한 프로세스와 별개로 완료 대기를 위해 스레드 사용
    import threading
    threading.Thread(target=_wait_and_save, daemon=True).start()


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

    # 좌표 직접 지정이면 지오코딩 없이 분석 시작
    lat = body.get("lat")
    lng = body.get("lng")
    if lat is not None and lng is not None:
        try:
            lat_f = float(lat)
            lng_f = float(lng)
        except (TypeError, ValueError):
            return (
                jsonify(
                    {"error": "위경도 값이 잘못됐습니다. 숫자 lat, lng를 보내 주세요.", "status": 400}
                ),
                400,
                {"Content-Type": "application/json; charset=utf-8"},
            )
        place_str = body.get("place") or f"{lat_f}, {lng_f}"
        bbox = (lat_f - 0.015, lng_f - 0.015, lat_f + 0.015, lng_f + 0.015)
        use_cache = _bboxes_overlap(bbox, SUNCHOON_BBOX)
        job_id = uuid.uuid4().hex
        _save_job(job_id, {
            "status": "queued",
            "place": place_str,
            "bbox": list(bbox),
        })
        _start_job(job_id, {"bbox": list(bbox), "place": place_str}, use_cache)
        return (
            jsonify({"status": 200, "job_id": job_id, "place": place_str}),
            200,
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

    lat_f, lng_f = loc
    bbox = (lat_f - 0.015, lng_f - 0.015, lat_f + 0.015, lng_f + 0.015)
    use_cache = _bboxes_overlap(bbox, SUNCHOON_BBOX)
    job_id = uuid.uuid4().hex
    _save_job(job_id, {
        "status": "queued",
        "place": place,
        "bbox": list(bbox),
    })
    _start_job(job_id, {"bbox": list(bbox), "place": place}, use_cache)
    return (
        jsonify({"status": 200, "job_id": job_id, "place": place}),
        200,
        {"Content-Type": "application/json; charset=utf-8"},
    )


@app.route("/api/result/<job_id>", methods=["GET"])
def result(job_id):
    job = _load_job(job_id)
    if job is None:
        return (
            jsonify({"error": "작업 ID를 찾을 수 없습니다.", "status": 404}),
            404,
            {"Content-Type": "application/json; charset=utf-8"},
        )

    if job.get("status") == "done":
        return (
            jsonify(
                {
                    "status": 200,
                    "job_id": job_id,
                    "place": job.get("place", ""),
                    "bbox": job.get("bbox", []),
                    "markdown": job.get("markdown", ""),
                }
            ),
            200,
            {"Content-Type": "application/json; charset=utf-8"},
        )

    if job.get("status") == "error":
        return (
            jsonify(
                {
                    "status": "error",
                    "job_id": job_id,
                    "error": job.get("error", "알 수 없는 오류"),
                }
            ),
            200,
            {"Content-Type": "application/json; charset=utf-8"},
        )

    # running / queued
    return (
        jsonify(
            {
                "status": job.get("status", "running"),
                "job_id": job_id,
            }
        ),
        200,
        {"Content-Type": "application/json; charset=utf-8"},
    )


@app.route("/api/geocode", methods=["POST"])
def geocode_endpoint():
    body = request.get_json(silent=True) or {}
    lat = body.get("lat")
    lng = body.get("lng")
    if lat is None or lng is None:
        return (
            jsonify({"error": "lat, lng가 필요합니다.", "status": 400}),
            400,
            {"Content-Type": "application/json; charset=utf-8"},
        )
    try:
        lat_f = float(lat)
        lng_f = float(lng)
    except (TypeError, ValueError):
        return (
            jsonify({"error": "lat, lng는 숫자여야 합니다.", "status": 400}),
            400,
            {"Content-Type": "application/json; charset=utf-8"},
        )

    address = _rev_geocode(lat_f, lng_f)
    if not address:
        return (
            jsonify({"error": "주소를 찾지 못했습니다.", "status": 404}),
            404,
            {"Content-Type": "application/json; charset=utf-8"},
        )

    return (
        jsonify({"address": address}),
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
