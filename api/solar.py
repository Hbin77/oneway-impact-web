"""
Solar Pro 4 Chat Completions 클라이언트 (OpenAI 호환)

문서 방식:
- 엔드포인트: https://api.upstage.ai/v1/chat/completions
- 인증: Bearer UPSTAGE_API_KEY (서버 환경변수만)
- 모델: solar-pro4 (하나만)
- 도구 호출: docs 방식 (tools + tool_choice)
- 구조화 출력: response_format json_schema + strict:true
  (strict:true, additionalProperties:false(모든 객체), required에 전 프로퍼티)
- 응답 JSON은 choices[0].message.content 문자열. finish_reason=length면 잘린 것.
- 도구 결과 메시지는 docs 예시처럼 role: tool + name + tool_call_id로 전달.

실장은 표준 라이브러리만 사용한다.
"""
import json
import os
import urllib.request
import urllib.error
import logging

LOG = logging.getLogger("solar")

API_URL = "https://api.upstage.ai/v1/chat/completions"
MODEL = "solar-pro4"

# 도구 정의 (docs 방식)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "analyze",
            "description": (
                "지명/장소의 도로 일방통행 영향도 분석을 시작한다. "
                "실제 엔진 실행은 서버(/api/analyze)가 job_id로 비동기 수행한다. "
                "이 도구는 '분석을 시작하라'는 지시만 담으며, 호출자는 job_id를 받아 "
                "완료될 때까지 폴링해야 한다. 수치·좌표는 만들지 않는다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "place": {
                        "type": "string",
                        "description": "분석할 지명/장소 (예: '순천 원도심').",
                    }
                },
                "required": ["place"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "solar_respond",
            "description": (
                "완료된 분석 결과(엔진 markdown)와 사용자의 질문을 근거로 자연어 답변을 작성한다. "
                "엔진 수치를 바꾸거나 새 수치·좌표를 만들지 않는다. "
                "답변은 한국어, 근거는 job_id를 항상 표시한다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "엔진 분석 작업의 job_id.",
                    },
                    "markdown": {
                        "type": "string",
                        "description": "엔진 보고서의 마크다운 원문 (수치가 포함된 유일한 근거).",
                    },
                    "question": {
                        "type": "string",
                        "description": "사용자가 던진 질문.",
                    },
                },
                "required": ["job_id", "markdown", "question"],
                "additionalProperties": False,
            },
        },
    },
]


def _env_key() -> str:
    return os.environ.get("UPSTAGE_API_KEY", "")


def _post(payload: dict, timeout: float = 30.0):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {_env_key()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        LOG.warning("Solar HTTP 오류 %s: %s", e.code, body[:500])
        return {"error": f"업스테이지 API 오류({e.code})", "raw": body[:1000]}
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        LOG.warning("Solar 연결 실패: %s", e)
        return {"error": "업스테이지 API 연결 실패", "raw": str(e)}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        LOG.warning("Solar 응답 파싱 실패: %s", raw[:500])
        return {"error": "Solar 응답 파싱 실패", "raw": raw[:1000]}


def chat_completion(
    messages: list,
    *,
    tools: list | None = None,
    tool_choice: str | None = None,
    response_format: dict | None = None,
    timeout: float = 30.0,
):
    """
    Solar Chat Completions 단일 턴.

    - tools: docs 방식 tool calling
    - response_format: docs 방식 구조화 출력 (json_schema + strict)

    반환: {"error": ...} 또는 {"choices": [...], ...}
    """
    if not _env_key():
        return {"error": "UPSTAGE_API_KEY가 설정돼 있지 않습니다."}

    payload = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": 4096,
    }
    if tools is not None:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    if response_format is not None:
        payload["response_format"] = response_format

    return _post(payload, timeout=timeout)


# 구조화 출력 스키마: 엔진 결과를 근거로 한 최종 응답
# 요구사항: root object, strict:true, additionalProperties:false(모든 객체),
# required에 전 프로퍼티, 옵션 값은 null 타입. 중첩 10레벨 이하.
RESPONSE_SCHEMA = {
    "name": "agent_reply",
    "description": "엔진 분석 결과를 근거로 한 사용자 질문 답변.",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string",
                        "description": "사용자 질문에 대한 한 줄 요약 (엔진 수치를 바꾸지 말 것)."},
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "from_table": {"type": "string",
                                       "description": "근거가 된 표/항목 이름."},
                        "value": {"type": "string",
                                  "description": "엔진 원문에서 인용한 값(그대로)."},
                    },
                    "required": ["from_table", "value"],
                    "additionalProperties": False,
                },
                "description": "답변을 떠받치는 엔진 표·값 목록. 엔진 markdown에서 그대로 인용.",
            },
            "note": {"type": "string",
                     "description": "해석 주의·한계 (엔진 고지사항과 충돌하지 않게)."},
            "job_id": {"type": "string",
                       "description": "근거가 된 엔진 분석 작업의 job_id."},
        },
        "required": ["summary", "evidence", "note", "job_id"],
        "additionalProperties": False,
    },
}


def structured_reply(job_id: str, markdown: str, question: str) -> dict:
    """
    엔진 완료 마크다운 + 질문을 Solar에 넣어 구조화된 응답을 얻는다.

    성공 시: {"ok": True, "reply": <RESPONSE_SCHEMA 파싱 dict>}
    실패 시: {"ok": False, "error": "..."}
    """
    system = (
        "너는 도로 일방통행 영향도 분석 결과를 읽고 사용자 질문에 답하는 보조 도구다.\n"
        "유일한 수치 근거는 엔진 보고서(markdown)다. 수치를 바꾸거나 새로 만들지 말고, "
        "엔진 markdown에서 있는 값만 인용하라.\n"
        "답변은 한국어로 짧게, 근거는 표 이름과 인용값을 evidence에 적고, "
        "job_id를 항상 표시하라.\n"
        "해석 주의(통행시간은 합성 OD 기반 상대 비교, 상권 접근성은 두 축만, 조합은 탐욕 국소해 등)는 "
        "엔진 고지사항을 그대로 따른다.\n"
        "앞의 '기존 공식 일방통행 지정 현황' 블록은 경찰청 전국일방통행도로표준데이터(공공데이터포털)이며, "
        "인용 시 evidence.from_table에 그 출처와 참조일을 적고, 엔진 수치와 섞거나 바꾸지 않는다."
    )
    user = (
        f"## 엔진 보고서 (job_id: {job_id})\n\n{markdown}\n\n"
        f"## 사용자 질문\n\n{question}\n\n"
        "위 보고서를 근거로 질문에 답하라. 위 RESPONSE_SCHEMA에 맞춰 JSON만 반환하라."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    resp = chat_completion(
        messages,
        response_format={
            "type": "json_schema",
            "json_schema": RESPONSE_SCHEMA,
        },
        timeout=90.0,
    )
    if "error" in resp:
        return {"ok": False, "error": resp["error"]}

    choices = resp.get("choices", [])
    if not choices:
        return {"ok": False, "error": "Solar 응답에 choices가 없습니다.", "raw": resp}
    first = choices[0]
    finish = first.get("finish_reason")
    if finish == "length":
        return {
            "ok": False,
            "error": "Solar 응답이 max_tokens에 도달해 잘렸습니다. max_tokens를 늘리거나 다시 시도하세요.",
            "raw": resp,
        }
    if finish != "stop":
        return {
            "ok": False,
            "error": f"Solar 응답 finish_reason이 예상과 다릅니다: {finish!r}",
            "raw": resp,
        }

    msg = first.get("message", {})
    content = msg.get("content", "")
    if not content:
        return {"ok": False, "error": "Solar 응답이 비어 있습니다.", "raw": resp}
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return {"ok": False, "error": "Solar 응답을 JSON으로 파싱하지 못했습니다.", "raw": content[:1000]}
    return {"ok": True, "reply": parsed}


def tool_roundtrip(place: str, question: str, markdown: str, job_id: str) -> dict:
    """
    Solar가 도구 호출 방식으로 응답해야 할 때 쓰는 헬퍼.

    문서 예시 흐름:
    1) 첫 호출: tools + tool_choice 전달
    2) tool_call 수신 → 서버 측에서 실행 결과 정리(job_id/markdown/question)
    3) 두 번째 호출: messages + [assistant 메시지, tool 결과] 전달
       tool 결과 메시지는 docs 예시처럼 role: tool + name + tool_call_id로 전달
    4) 최종 텍스트 응답 반환

    실패 시 error dict를 반환한다. 이 경로는 주 경로가 아니며,
    구조화 출력(structured_reply)이 우선한다.
    """
    if not _env_key():
        return {"error": "UPSTAGE_API_KEY가 설정돼 있지 않습니다."}

    system = (
        "너는 도로 일방통행 영향도 분석 서비스를 돕는 도구다. "
        "사용자 질문이 들어오면 필요하면 analyze 도구로 분석을 시작하고, "
        "분석이 끝나면 solar_respond 도구로 답변을 작성한다.\n"
        "분석 job은 서버(/api/analyze)가 비동기로 수행하며, 이 도구 호출은 "
        "'분석을 시작하라'는 지시만 담는다. job_id를 받으면 완료될 때까지 폴링해야 한다.\n"
        "수치는 엔진 보고서에만 있고, 너는 새 수치를 만들지 않는다."
    )
    user = (
        f"장소: {place}\n"
        f"질문: {question}\n"
        "분석을 시작해도 되면 analyze를 호출하고, 분석할 내용이 이미 주어졌거나 "
        "완료된 결과를 설명·구조화해야 하면 solar_respond를 호출하라."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    first = chat_completion(
        messages,
        tools=TOOLS,
        tool_choice="required",
        timeout=90.0,
    )
    if "error" in first:
        return {"error": first["error"]}

    msg = first.get("choices", [{}])[0].get("message", {})
    tool_calls = msg.get("tool_calls") or []
    if not tool_calls:
        content = msg.get("content", "") or "(응답 없음)"
        return {"ok": True, "reply_text": content}

    tc = tool_calls[0]
    fn = tc.get("function", {})
    name = fn.get("name")
    args = {}
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except (json.JSONDecodeError, ValueError):
        return {"error": "도구 인자 파싱 실패"}

    call_id = tc.get("id", "call_1")

    def _tool_result_content() -> str:
        if name == "analyze":
            return json.dumps({
                "status": "started",
                "job_id": job_id,
                "place": place,
                "note": "실제 분석 결과는 /api/agent/<job_id> 또는 /api/result/<job_id>에서 폴링으로 확인한다.",
            })
        if name == "solar_respond":
            return json.dumps({
                "job_id": job_id,
                "markdown": markdown,
                "question": question,
            })
        return json.dumps({"error": f"알 수 없는 도구: {name}"})

    # docs 예시: tool 결과 메시지는 role: tool, name, tool_call_id을 포함한다
    result_msg = {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": _tool_result_content(),
    }

    second = chat_completion(
        messages + [msg, result_msg],
        timeout=90.0,
    )
    if "error" in second:
        return {"error": second["error"]}
    final = second.get("choices", [{}])[0].get("message", {})
    return {"ok": True, "reply_text": final.get("content", "") or "(응답 없음)"}
