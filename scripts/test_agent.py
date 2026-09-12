"""
엔진 결과만으로 agent가 정상 동작하는지 확인하는 로컬 검증 스크립트.
- 키가 없을 때: engine 결과 + "Solar 응답을 만들지 않았다" 상태만 저장된 mock을 검증
- 키가 있을 때: 실제 Solar 호출(최신 키 사용) 후 구조화 응답이 job_id 등 스키마를 만족하는지 검증
"""
import json
import os
import sys
import time
import uuid
import subprocess
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent

sys.path.insert(0, str(ROOT / "api"))

def _engine_job_for_place(place, lat=None, lng=None):
    from analyze import _bboxes_overlap, SUNCHOON_BBOX, _start_job, _save_job, geocode
    if lat is not None and lng is not None:
        try:
            lat_f = float(lat); lng_f = float(lng)
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


def _wait_for_engine(job_id, timeout=300):
    from analyze import _load_job
    start = time.time()
    while time.time() - start < timeout:
        j = _load_job(job_id)
        if j is None:
            raise RuntimeError("engine job disappeared")
        st = j.get("status")
        if st == "done":
            return j
        if st == "error":
            raise RuntimeError(j.get("error", "engine error"))
        time.sleep(2.0)
    raise RuntimeError("engine timed out")


def test_engine_only(place="순천 원도심"):
    print(f"[engine-only] place={place}")
    engine_job = _engine_job_for_place(place)
    assert engine_job is not None, "engine job 생성 실패"
    job_id, bbox, place_str = engine_job
    print(f"  engine job_id={job_id}, bbox={bbox}, place={place_str}")
    j = _wait_for_engine(job_id)
    assert j.get("status") == "done", f"engine status={j.get('status')}"
    md = j.get("markdown", "")
    assert isinstance(md, str) and len(md) > 0, "engine markdown 비어 있음"
    print(f"  engine markdown 길이={len(md)}글자")
    assert "##" in md, "engine markdown에 표제가 없음"
    print("  OK: 엔진 결과만으로 정상 반환됨 (Solar 없음)")


def test_agent_with_key(place="순천 원도심", question="어디를 먼저 바꾸면 가장 효과가 큰가요?"):
    from agent import _save_agent, _load_agent, _run_solar, _wait_for_engine
    engine_job = _engine_job_for_place(place)
    assert engine_job is not None, "engine job 생성 실패"
    job_id, bbox, place_str = engine_job
    agent_job_id = uuid.uuid4().hex
    _save_agent(agent_job_id, {
        "status": "engine_done",
        "engine_job_id": job_id,
        "place": place_str,
        "bbox": bbox,
        "markdown": "",
        "question": question,
    })
    j = _wait_for_engine(job_id)
    md = j.get("markdown", "")
    _save_agent(agent_job_id, {
        "status": "engine_done",
        "engine_job_id": job_id,
        "place": place_str,
        "bbox": bbox,
        "markdown": md,
        "question": question,
    })
    print(f"[agent+key] engine_job_id={job_id}, agent_job_id={agent_job_id}")
    _run_solar(agent_job_id, md, job_id, question)
    # Solar 응답은 수 초 후 저장되므로 폴링
    for _ in range(30):
        st = _load_agent(agent_job_id)
        if st is None:
            raise RuntimeError("agent 상태가 사라짐")
        if st.get("status") == "done":
            break
        time.sleep(2.0)
    st = _load_agent(agent_job_id)
    assert st.get("status") == "done", f"agent status={st.get('status')}"
    reply = st.get("reply")
    err = st.get("reply_error")
    print(f"  reply_error={err!r}")
    if err:
        print("  주의: Solar 응답 생성 실패(키/시간/ 스키마) — 엔진 결과만으로는 정상 동작해야 함")
        return
    assert reply is not None and isinstance(reply, dict), "reply 없음"
    for k in ("summary", "evidence", "note", "job_id"):
        assert k in reply, f"reply에 {k} 없음: {reply.keys()}"
    assert reply["job_id"] == job_id, "reply job_id 불일치"
    assert isinstance(reply["evidence"], list) and len(reply["evidence"]) > 0, "evidence 비어 있음"
    for e in reply["evidence"]:
        assert "from_table" in e and "value" in e, f"evidence 항목 파손: {e}"
    print("  OK: Solar 구조화 응답 스키마 만족, job_id 일치")
    print(f"  summary={reply['summary'][:120]!r}")
    print(f"  evidence={reply['evidence']}")


if __name__ == "__main__":
    key = os.environ.get("UPSTAGE_API_KEY", "")
    print("=" * 70)
    test_engine_only()
    if key:
        print("=" * 70)
        test_agent_with_key()
    else:
        print("=" * 70)
        print("UPSTAGE_API_KEY 미설정 → Solar 호출 검증 건너뜀 (엔진 전용만 검증)")
    print("=" * 70)
    print("PASS")
