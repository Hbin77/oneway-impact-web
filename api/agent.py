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
from analyze import _google_geocode

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
    """기존 analyze.py의 로직을 재사용해 engine job을 시작하고 (job_id, bbox, place, address)를 반환."""
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
        return job_id, list(bbox), place_str, ""

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
    _, _, address = _google_geocode(place_str) or ("", "", "")
    _save_job(job_id, {"status": "queued", "place": place_str, "bbox": list(bbox), "address": address})
    _start_job(job_id, {"bbox": list(bbox), "place": place_str, "address": address}, use_cache)
    return job_id, list(bbox), place_str, address


def _designations_summary_block(ons: dict) -> str:
    """Solar 프롬프트 앞에 붙일 기존 공식 일방통행 지정 현황 요약 블록.

    items 전체는 넣지 않고 건수·범위·도로명 일부·source만 요약한다.
    """
    if not isinstance(ons, dict) or ons.get("count", 0) == 0:
        return ""

    count = ons.get("count", 0)
    road_bt_min = ons.get("roadBt_min")
    road_bt_max = ons.get("roadBt_max")
    road_et_min = ons.get("roadEt_min")
    road_et_max = ons.get("roadEt_max")
    appn_min = ons.get("appnYear_min")
    appn_max = ons.get("appnYear_max")
    source = ons.get("source", "경찰청 전국일방통행도로표준데이터(공공데이터포털)")
    sample = ons.get("sample", []) or []
    road_names = []
    for row in sample[:5]:
        nm = row.get("roadNm", "").strip() if isinstance(row, dict) else ""
        if nm:
            road_names.append(nm)
    names_txt = ", ".join(road_names) if road_names else "없음"

    parts = [
        "## 기존 공식 일방통행 지정 현황 (별도 출처 — 아래 표와 engine 계산 수치는 별개)",
        f"- 출처: {source}",
        f"- 구역 내 기존 공식 일방통행 지정: {count}건",
    ]
    if road_bt_min is not None and road_bt_max is not None:
        parts.append(f"- 도로 폭 범위: {road_bt_min}~{road_bt_max}m")
    if road_et_min is not None and road_et_max is not None:
        parts.append(f"- 도로 연장 범위: {road_et_min}~{road_et_max}m")
    if appn_min is not None and appn_max is not None:
        parts.append(f"- 지정연도 범위: {appn_min}~{appn_max}년")
    parts.append(f"- 도로명(일부): {names_txt}")
    parts.append("- 위 수치는 경찰청 전국일방통행도로표준데이터(공공데이터포털)이며, 엔진 계산에는 사용하지 않는다.")
    return "\n".join(parts)


def _designations_summary_block(ons: dict) -> str:
    """Solar 프롬프트 앞에 붙일 기존 공식 일방통행 지정 현황 요약 블록.

    items 전체는 넣지 않고 건수·범위·도로명 일부·source만 요약한다.
    """
    if not isinstance(ons, dict) or ons.get("count", 0) == 0:
        return ""

    count = ons.get("count", 0)
    road_bt_min = ons.get("roadBt_min")
    road_bt_max = ons.get("roadBt_max")
    road_et_min = ons.get("roadEt_min")
    road_et_max = ons.get("roadEt_max")
    appn_min = ons.get("appnYear_min")
    appn_max = ons.get("appnYear_max")
    source = ons.get("source", "경찰청 전국일방통행도로표준데이터(공공데이터포털)")
    sample = ons.get("sample", []) or []
    road_names = []
    for row in sample[:5]:
        nm = row.get("roadNm", "").strip() if isinstance(row, dict) else ""
        if nm:
            road_names.append(nm)
    names_txt = ", ".join(road_names) if road_names else "없음"

    parts = [
        "## 기존 공식 지정 현황 (별도 출처 — 아래 표·engine 계산 수치와 별개)",
        f"- 출처: {source}",
        f"- 구역 내 기존 공식 일방통행 지정: {count}건",
    ]
    if road_bt_min is not None and road_bt_max is not None:
        parts.append(f"- 도로 폭 범위: {road_bt_min}~{road_bt_max}m")
    if road_et_min is not None and road_et_max is not None:
        parts.append(f"- 도로 연장 범위: {road_et_min}~{road_et_max}m")
    if appn_min is not None and appn_max is not None:
        parts.append(f"- 지정연도 범위: {appn_min}~{appn_max}년")
    parts.append(f"- 도로명(일부): {names_txt}")
    parts.append("- 위 수치는 경찰청 전국일방통행도로표준데이터(공공데이터포털)이며, 엔진 계산에는 사용하지 않는다.")
    return "\n".join(parts)


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
                ej = _load_job(job_id)
                ons = ej.get("oneway_designations") if ej else None
                if not isinstance(ons, dict):
                    ons = {"count": 0, "items": []}
                _save_agent(agent_job_id, {
                    "status": "engine_done",
                    "engine_job_id": job_id,
                    "place": ej.get("place", ""),
                    "bbox": ej.get("bbox", []),
                    "markdown": ej.get("markdown", ""),
                    "engine_error": ej.get("error"),
                    "oneway_designations": ons,
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


def _designations_summary_block(ons: dict) -> str:
    """Solar 프롬프트 앞에 붙일 기존 공식 일방통행 지정 현황 요약 블록.

    items 전체는 넣지 않고 건수·범위·도로명 일부·source만 요약한다.
    """
    if not isinstance(ons, dict) or ons.get("count", 0) == 0:
        return ""

    count = ons.get("count", 0)
    road_bt_min = ons.get("roadBt_min")
    road_bt_max = ons.get("roadBt_max")
    road_et_min = ons.get("roadEt_min")
    road_et_max = ons.get("roadEt_max")
    appn_min = ons.get("appnYear_min")
    appn_max = ons.get("appnYear_max")
    source = ons.get("source", "경찰청 전국일방통행도로표준데이터(공공데이터포털)")
    sample = ons.get("sample", []) or []
    road_names = []
    for row in sample[:5]:
        nm = row.get("roadNm", "").strip() if isinstance(row, dict) else ""
        if nm:
            road_names.append(nm)
    names_txt = ", ".join(road_names) if road_names else "없음"

    parts = [
        "## 기존 공식 일방통행 지정 현황 (별도 출처 — 아래 표와 engine 계산 수치는 별개)",
        f"- 출처: {source}",
        f"- 구역 내 기존 공식 일방통행 지정: {count}건",
    ]
    if road_bt_min is not None and road_bt_max is not None:
        parts.append(f"- 도로 폭 범위: {road_bt_min}~{road_bt_max}m")
    if road_et_min is not None and road_et_max is not None:
        parts.append(f"- 도로 연장 범위: {road_et_min}~{road_et_max}m")
    if appn_min is not None and appn_max is not None:
        parts.append(f"- 지정연도 범위: {appn_min}~{appn_max}년")
    parts.append(f"- 도로명(일부): {names_txt}")
    parts.append("- 위 수치는 경찰청 전국일방통행도로표준데이터(공공데이터포털)이며, 엔진 계산에는 사용하지 않는다.")
    return "\n".join(parts)


def _run_solar(agent_job_id, markdown, job_id, question, oneway_designations):
    """Solar 응답을 시도하고 결과를 agent 상태에 저장."""
    # 기존 공식 일방통행 지정 현황 요약 블록을 엔진 마크다운 앞에 붙인다.
    # items 전체는 넣지 않는다(컨텍스트 과다·키 노출 우려).
    pre = _designations_summary_block(oneway_designations)
    enhanced_md = pre + "\n\n" + markdown if pre else markdown

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

    job_id, bbox, place_str, _addr = engine_job
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
            ons = st.get("oneway_designations", {"count": 0, "items": []})
            _run_solar(agent_job_id, md, job_id, q, ons)

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
        if isinstance(st.get("oneway_designations"), dict):
            out["oneway_designations"] = st.get("oneway_designations")
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
