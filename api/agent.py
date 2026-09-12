"""
에이전트 라우트 — Solar Pro 4 도구/구조화 출력을 쓴다.

흐름:
- POST /api/agent  {place, question}  → 엔진 analyze를 시작해 engine job_id를 만들고,
  agent job_id를 반환한다. 실제 엔진 실행과 Solar 응답은 비동기로 진행된다.
- GET /api/agent/<job_id>            → 진행 상태. 완료 시 Solar 응답(구조화/텍스트)과
  engine job_id, markdown을 함께 반환한다.

제약:
- 모델: solar-pro4 (하나만, api/solar.py의 MODEL과 동일 문자열)
- Solar 호출은 서버 /api에서만, 키는 UPSTAGE_API_KEY(서버 환경변수)만
- 엔진(api/road_impact.py)과 suncheon_network.json은 수정하지 않는다
- 엔진 출력이 유일한 수치 근거. LLM은 설명·구조화만 하고 수치를 바꾸지 않는다
- LLM 호출 실패/타임아웃 시 엔진 결과만으로 정상 동작해야 한다

엔드포인트는 기존 분석 Flask 앱(analyze.app)에 직접 레지스터한다.
"""
import json
import os
import threading
import time
import uuid
from pathlib import Path

from flask import request, jsonify

import analyze
from analyze import _bboxes_overlap, SUNCHOON_BBOX, _start_job, _save_job, _load_job, geocode

app = analyze.app

AGENT_DIR = Path("/tmp/oneway_agent_jobs")
AGENT_DIR.mkdir(parents=True, exist_ok=True)

POLL_MS = 2500
MAX_AGENT_POLLS = 480          # 약 20분 (엔진 최대 5분 + Solar 여유)


def _agent_path(job_id):
    return AGENT_DIR / f"{job_id}.json"


def _save_agent(job_id, state):
    _agent_path(job_id).write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8"
    )


def _load_agent(job_id):
    p = _agent_path(job_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _engine_job_for_place(place, lat=None, lng=None):
    """기존 analyze.py의 로직을 재사용해 engine job을 시작하고 (job_id, bbox, place)를 반환."""
    if lat is not None and lng is not None:
        try:
            lat_f = float(lat)
            lng_f = float(lng)
        except (TypeError, ValueError):
            return None
        bbox = (lat_f - 0.015, lng_f - 0.015, lat_f + 0.015, lng_f + 0.015)
        use_cache = _bboxes_overlap(bbox, SUNCHOON_BBOX)
        place_str = (place or f"{lat_f}, {lng_f}") or f"{lat_f}, {lng_f}"
        job_id = uuid.uuid4().hex
        _save_job(job_id, {"status": "queued", "place": place_str, "bbox": list(bbox)})
        _start_job(job_id, {"bbox": list(bbox), "place": place_str}, use_cache)
        return job_id, list(bbox), place_str

    place_str = (place or "").strip()
    if not place_str:
        return None
    loc = geocode(place_str)
    if loc is None:
        return None
    lat_f, lng_f = loc
    bbox = (lat_f - 0.015, lng_f - 0.015, lat_f + 0.015, lng_f + 0.015)
    use_cache = _bboxes_overlap(bbox, SUNCHOON_BBOX)
    job_id = uuid.uuid4().hex
    _save_job(job_id, {"status": "queued", "place": place_str, "bbox": list(bbox)})
    _start_job(job_id, {"bbox": list(bbox), "place": place_str}, use_cache)
    return job_id, list(bbox), place_str


def _wait_for_engine(job_id, agent_job_id):
    """engine job 완료까지 폴링하고 결과를 agent 상태에 저장."""
    try:
        for _ in range(MAX_AGENT_POLLS):
            ej = _load_job(job_id)
            if ej is None:
                _save_agent(agent_job_id, {
                    "status": "error",
                    "error": "엔진 작업 상태를 읽지 못했습니다.",
                    "engine_job_id": job_id,
                })
                return
            st = ej.get("status")
            if st == "done":
                _save_agent(agent_job_id, {
                    "status": "engine_done",
                    "engine_job_id": job_id,
                    "place": ej.get("place", ""),
                    "bbox": ej.get("bbox", []),
                    "markdown": ej.get("markdown", ""),
                    "engine_error": ej.get("error"),
                })
                return
            if st == "error":
                _save_agent(agent_job_id, {
                    "status": "error",
                    "error": ej.get("error", "엔진 분석이 실패했습니다."),
                    "engine_job_id": job_id,
                })
                return
            time.sleep(POLL_MS / 1000.0)
        _save_agent(agent_job_id, {
            "status": "error",
            "error": "엔진 분석이 정해진 시간 안에 끝나지 않았습니다.",
            "engine_job_id": job_id,
        })
    except Exception as exc:
        _save_agent(agent_job_id, {
            "status": "error",
            "error": f"엔진 결과 대기 중 오류: {exc}",
            "engine_job_id": job_id,
        })


def _run_solar(agent_job_id, markdown, job_id, question):
    """Solar 응답을 시도하고 결과를 agent 상태에 저장."""
    try:
        import solar
    except Exception as exc:
        _save_agent(agent_job_id, {
            "status": "done",
            "engine_job_id": job_id,
            "markdown": markdown,
            "place": _load_agent(agent_job_id).get("place", ""),
            "bbox": _load_agent(agent_job_id).get("bbox", []),
            "reply": None,
            "reply_error": f"Solar 모듈 로딩 실패(LLM 미사용): {exc}",
        })
        return

    key = os.environ.get("UPSTAGE_API_KEY", "")
    if not key:
        _save_agent(agent_job_id, {
            "status": "done",
            "engine_job_id": job_id,
            "markdown": markdown,
            "place": _load_agent(agent_job_id).get("place", ""),
            "bbox": _load_agent(agent_job_id).get("bbox", []),
            "reply": None,
            "reply_error": "UPSTAGE_API_KEY가 설정돼 있지 않아 Solar 응답을 만들지 않았습니다. 엔진 결과만 표시됩니다.",
        })
        return

    got = solar.structured_reply(job_id, markdown, question)
    if not got.get("ok"):
        _save_agent(agent_job_id, {
            "status": "done",
            "engine_job_id": job_id,
            "markdown": markdown,
            "place": _load_agent(agent_job_id).get("place", ""),
            "bbox": _load_agent(agent_job_id).get("bbox", []),
            "reply": None,
            "reply_error": got.get("error", "Solar 응답 생성 실패"),
        })
        return

    reply = got["reply"]
    if isinstance(reply, dict):
        reply.setdefault("job_id", job_id)
    _save_agent(agent_job_id, {
        "status": "done",
        "engine_job_id": job_id,
        "markdown": markdown,
        "place": _load_agent(agent_job_id).get("place", ""),
        "bbox": _load_agent(agent_job_id).get("bbox", []),
        "reply": reply,
        "reply_error": None,
    })


@app.route("/api/agent", methods=["POST"])
def agent_start():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return (
            jsonify({"error": "요청 형식이 잘못됐습니다. JSON 객체를 보내 주세요."}),
            400,
            {"Content-Type": "application/json; charset=utf-8"},
        )

    place = (body.get("place") or "").strip()
    question = (body.get("question") or "").strip()
    lat = body.get("lat")
    lng = body.get("lng")

    if not place and not (lat is not None and lng is not None):
        return (
            jsonify({
                "error": "지명이나 위경도를 입력해 주세요.",
                "status": 400,
            }),
            400,
            {"Content-Type": "application/json; charset=utf-8"},
        )

    engine_job = _engine_job_for_place(place, lat, lng)
    if engine_job is None:
        return (
            jsonify({
                "error": f"'{place or '입력값'}' 위치를 찾지 못했습니다. 더 구체적인 지명이나 주소를 입력해 주세요.",
                "status": 404,
            }),
            404,
            {"Content-Type": "application/json; charset=utf-8"},
        )

    job_id, bbox, place_str = engine_job
    agent_job_id = uuid.uuid4().hex
    _save_agent(agent_job_id, {
        "status": "queued",
        "engine_job_id": job_id,
        "place": place_str,
        "bbox": bbox,
        "question": question,
        "started_at": time.time(),
    })

    def _run():
        _wait_for_engine(job_id, agent_job_id)
        st = _load_agent(agent_job_id)
        if st and st.get("status") == "engine_done":
            md = st.get("markdown", "")
            q = st.get("question", "")
            _run_solar(agent_job_id, md, job_id, q)

    threading.Thread(target=_run, daemon=True).start()

    return (
        jsonify({
            "status": 200,
            "agent_job_id": agent_job_id,
            "engine_job_id": job_id,
            "place": place_str,
            "question": question,
            "status_detail": "queued",
        }),
        200,
        {"Content-Type": "application/json; charset=utf-8"},
    )


@app.route("/api/agent/<agent_job_id>", methods=["GET"])
def agent_status(agent_job_id):
    st = _load_agent(agent_job_id)
    if st is None:
        return (
            jsonify({"error": "에이전트 작업 ID를 찾을 수 없습니다.", "status": 404}),
            404,
            {"Content-Type": "application/json; charset=utf-8"},
        )

    status = st.get("status", "unknown")

    out = {
        "agent_job_id": agent_job_id,
        "status": status,
        "engine_job_id": st.get("engine_job_id"),
        "place": st.get("place", ""),
        "bbox": st.get("bbox", []),
        "question": st.get("question", ""),
    }

    if status == "done":
        out["reply"] = st.get("reply")
        out["markdown"] = st.get("markdown", "")
        out["reply_error"] = st.get("reply_error")
    elif status == "error":
        out["error"] = st.get("error")
        out["markdown"] = st.get("markdown", "")
        out["reply_error"] = st.get("reply_error")
    else:
        out["status_detail"] = status

    return (
        jsonify(out),
        200,
        {"Content-Type": "application/json; charset=utf-8"},
    )
