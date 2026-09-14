"""
Rapid / Solar Pro 4 에이전트 인터페이스 (Upstage Chat Completions)

규정:
- 모델: upstage/solar-pro4 하나만 사용 (다른 모델·폴백·MoA 금지)
- 엔드포인트: https://api.upstage.ai/v1/chat/completions (OpenAI 호환)
- 인증: UPSTAGE_API_KEY 환경변수만 사용 (코드/로그/응답에 키 값 노출 금지)
- 도구 호출: Upstage docs 방식(tool calling) 사용
- 구조화 출력: JSON Schema + structured outputs 사용
- 재시도: 연결성 오류에만 제한적 적용, 인증/요청 오류는 즉시 중단
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


API_URL = "https://api.upstage.ai/v1/chat/completions"
MODEL = "upstage/solar-pro4"
ENV_KEY = "UPSTAGE_API_KEY"

REQUEST_TIMEOUT = 120.0
MAX_RETRIES = 3
MIN_BACKOFF = 2.0
MAX_BACKOFF = 20.0


@dataclass
class Invocation:
    attempt: int
    started_at: float
    finished_at: float
    ok: bool
    http_status: int | None
    error: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    raw: dict[str, Any] | None = None


@dataclass
class RunResult:
    invocations: list[Invocation] = field(default_factory=list)
    final_message: str = ""
    structured: dict[str, Any] | None = None
    stopped: bool = False
    stop_reason: str = ""


def _env_api_key() -> str:
    return os.environ.get(ENV_KEY, "").strip()


def _post(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {_env_api_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw)


def _extract_usage(raw: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        return None, None, None
    return (
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
        usage.get("total_tokens"),
    )


def _first_choice(raw: dict[str, Any]) -> dict[str, Any] | None:
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    return choices[0]


def _call_once(payload: dict[str, Any], timeout: float) -> tuple[dict[str, Any], float]:
    started = time.time()
    try:
        raw = _post(payload, timeout=timeout)
        finished = time.time()
        return raw, finished - started
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        raise RuntimeError(f"HTTP {exc.code}: {body[:500]}") from exc
    except Exception as exc:
        raise RuntimeError(f"request failed: {exc}") from exc


def chat_completion(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
    response_format: dict[str, Any] | None = None,
    timeout: float = REQUEST_TIMEOUT,
    max_retries: int = MAX_RETRIES,
) -> RunResult:
    """
    Solar Pro 4와 1회 왕복한다.
    - tools/tool_choice: Upstage docs 방식 도구 호출
    - response_format: 구조화 출력(JSON Schema + strict)
    키는 env에서만 읽고, 실패 시 재시도하되 인증/요청 오류는 바로 중단한다.
    """
    api_key = _env_api_key()
    result = RunResult()

    if not api_key:
        result.stop_reason = "missing_api_key"
        result.final_message = "UPSTAGE_API_KEY 환경변수가 설정되지 않아 Solar API를 호출할 수 없습니다."
        return result

    payload: dict[str, Any] = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": 4096,
        "temperature": 0.2,
    }
    if tools is not None:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    if response_format is not None:
        payload["response_format"] = response_format

    last_error: str | None = None
    backoff = MIN_BACKOFF
    for attempt in range(1, max_retries + 1):
        invocation = Invocation(
            attempt=attempt,
            started_at=time.time(),
            finished_at=time.time(),
            ok=False,
            http_status=None,
        )
        result.invocations.append(invocation)
        try:
            raw, elapsed = _call_once(payload, timeout=timeout)
            invocation.finished_at = invocation.started_at + elapsed
            invocation.ok = True
            invocation.raw = raw
            invocation.http_status = raw.get("http_status") or 200

            choice = _first_choice(raw)
            if choice is None:
                invocation.error = "응답에 choices가 없습니다."
                last_error = invocation.error
                backoff = min(backoff * 2, MAX_BACKOFF)
                time.sleep(backoff)
                continue

            invocation.http_status = raw.get("http_status") or 200
            pt, ct, tt = _extract_usage(raw)
            invocation.prompt_tokens = pt
            invocation.completion_tokens = ct
            invocation.total_tokens = tt

            message = choice.get("message", {})
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                result.final_message = content
                # 구조화 출력 요청이 있었으면 content를 JSON으로 파싱 시도
                if response_format is not None and "json_schema" in response_format:
                    try:
                        result.structured = json.loads(content)
                    except Exception:
                        result.structured = {"parse_error": True, "raw": content[:1000]}
                # 도구 호출이 반환됐으면 도구 호출 내역도 구조화 응답으로 남긴다
                tool_calls = message.get("tool_calls")
                if isinstance(tool_calls, list) and tool_calls:
                    result.structured = {
                        "tool_calls": [
                            {
                                "id": tc.get("id"),
                                "type": tc.get("type"),
                                "function": tc.get("function", {}),
                            }
                            for tc in tool_calls
                        ]
                    }
                break

            # content가 없으면 오류처럼 취급
            invocation.error = "응답 내용이 비어 있습니다."
            last_error = invocation.error
            backoff = min(backoff * 2, MAX_BACKOFF)
            time.sleep(backoff)

        except RuntimeError as exc:
            invocation.ok = False
            invocation.error = str(exc)
            last_error = invocation.error
            invocation.finished_at = invocation.started_at + (time.time() - invocation.started_at)
            backoff = min(backoff * 2, MAX_BACKOFF)
            time.sleep(backoff)

    if not result.final_message and result.invocations:
        last = result.invocations[-1]
        reason = last.error or "알 수 없는 오류"
        result.stop_reason = "api_error"
        result.final_message = f"Solar API 호출 실패: {reason}"
    elif not result.final_message:
        result.stop_reason = "no_response"
        result.final_message = "Solar 응답을 받지 못했습니다."

    return result


def summarize(result: RunResult, task_id: str) -> dict[str, Any]:
    """한 번의 작업 실행 결과를 JSON으로 정리한다."""
    calls = result.invocations
    if not calls:
        return {
            "task_id": task_id,
            "status": "no_invocations",
            "message": result.final_message,
        }

    success = all(c.ok for c in calls)
    total_prompt = sum(c.prompt_tokens for c in calls if c.prompt_tokens is not None)
    total_completion = sum(c.completion_tokens for c in calls if c.completion_tokens is not None)
    total_tokens = sum(c.total_tokens for c in calls if c.total_tokens is not None)

    return {
        "task_id": task_id,
        "status": "ok" if success else "error",
        "stop_reason": result.stop_reason or ("ok" if success else "api_error"),
        "model": MODEL,
        "api_endpoint": API_URL,
        "invocations": len(calls),
        "successful_invocations": sum(1 for c in calls if c.ok),
        "last_http_status": calls[-1].http_status,
        "last_error": calls[-1].error,
        "message": result.final_message,
        "structured": result.structured,
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_tokens": total_tokens,
        "timing_seconds": round(sum(c.finished_at - c.started_at for c in calls), 2),
    }


def summarize_history(history: list[dict[str, Any]]) -> dict[str, Any]:
    """작업 히스토리를 표 요약으로 정리한다."""
    rows = []
    for item in history:
        rows.append({
            "task_id": item.get("task_id"),
            "status": item.get("status"),
            "invocations": item.get("invocations"),
            "total_tokens": item.get("total_tokens"),
            "stop_reason": item.get("stop_reason"),
            "message_preview": (item.get("message") or "")[:200],
        })
    return {
        "history_count": len(rows),
        "summary": rows,
    }


def load_history(path: str = "rapid_history.json") -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_history(history: list[dict[str, Any]], path: str = "rapid_history.json") -> None:
    Path(path).write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------
#  프롬프트 구성
# --------------------------------------------------

TASK_CREATE_SYSTEM = (
    "너는 Road Engineering AI Agent다. 사용자는 도로 구간 전환 관련 작업을 요청한다. "
    "너의 역할은 요청을 이해하고, 실행 가능한 단일 작업(task)으로 정리한 뒤, "
    "남은 할 일을 구조화된 JSON으로 정리하는 것이다. "
    "수치 추정이나 외부 데이터 조회는 하지 말고, 요청의 의도와 범위만 정리해라."
)

TASK_CREATE_USER_TEMPLATE = """
## 사용자 요청
{query}

## 지시
위 요청을 바탕으로 아래 내용으로 작업을 생성해라.
- title: 작업을 한 줄로 요약 (한국어)
- objective: 작업의 목표 (한국어, 2~3문장)
- scope: 포함되는 범위와 제외되는 범위 (한국어)
- input_needed: 실제로 필요한 입력 목록 (문자열 배열)
- steps: 실행을 위한 단계별 계획 (문자열 배열)
- remaining_work: 요청 후 남아 있는 할 일 (문자열 배열)
- confidence: 이 정리가 요청을 얼마나 잘 반영했는지에 대한 자체 판단 ("high"/"medium"/"low")
- note: 해석상 주의나 가정 (한국어)

출력은 아래 JSON Schema에 맞춰 JSON만 반환해라.
"""


TASK_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "task",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "objective": {"type": "string"},
                "scope": {"type": "string"},
                "input_needed": {"type": "array", "items": {"type": "string"}},
                "steps": {"type": "array", "items": {"type": "string"}},
                "remaining_work": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "note": {"type": "string"},
            },
            "required": [
                "title",
                "objective",
                "scope",
                "input_needed",
                "steps",
                "remaining_work",
                "confidence",
                "note",
            ],
            "additionalProperties": False,
        },
    },
}


def build_task_create_prompt(query: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": TASK_CREATE_SYSTEM},
        {"role": "user", "content": TASK_CREATE_USER_TEMPLATE.format(query=query)},
    ]


# --------------------------------------------------
#  도구 정의 (선택적 사용)
# --------------------------------------------------

TASK_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": "사용자 요청을 작업(task) 구조로 정리한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "objective": {"type": "string"},
                    "scope": {"type": "string"},
                    "input_needed": {"type": "array", "items": {"type": "string"}},
                    "steps": {"type": "array", "items": {"type": "string"}},
                    "remaining_work": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "note": {"type": "string"},
                },
                "required": [
                    "title",
                    "objective",
                    "scope",
                    "input_needed",
                    "steps",
                    "remaining_work",
                    "confidence",
                    "note",
                ],
                "additionalProperties": False,
            },
        },
    }
]


def build_task_create_with_tool(query: str) -> dict[str, Any]:
    return {
        "messages": build_task_create_prompt(query),
        "tools": TASK_TOOLS,
        "tool_choice": "required",
        "response_format": TASK_SCHEMA,
    }


# --------------------------------------------------
#  메인 실행
# --------------------------------------------------

def run(
    query: str,
    *,
    use_tool: bool = True,
    history_path: str = "rapid_history.json",
) -> dict[str, Any]:
    """
    Solar Pro 4에 작업 생성 요청을 보내고 결과를 정리한다.
    - use_tool=True: 도구 호출 + 구조화 출력을 함께 사용
    - use_tool=False: 구조화 출력만 사용
    결과는 저장하지 않고 반환하며, 호출자는 필요시 히스토리에 추가한다.
    """
    if use_tool:
        request = build_task_create_with_tool(query)
        messages = request["messages"]
        tools = request["tools"]
        tool_choice = request["tool_choice"]
        response_format = request["response_format"]
    else:
        messages = build_task_create_prompt(query)
        tools = None
        tool_choice = None
        response_format = TASK_SCHEMA

    result = chat_completion(
        messages,
        tools=tools,
        tool_choice=tool_choice,
        response_format=response_format,
    )
    summary = summarize(result, task_id="rapid-" + str(int(time.time())))

    # 최종 메시지는 tool_call이 있으면 그 내용을 함께 보여준다
    if result.structured and isinstance(result.structured, dict) and "tool_calls" in result.structured:
        summary["tool_calls"] = result.structured["tool_calls"]

    return summary


def run_and_record(
    query: str,
    *,
    use_tool: bool = True,
    history_path: str = "rapid_history.json",
) -> dict[str, Any]:
    """실행 후 히스토리에 저장하고, 저장된 히스토리의 요약도 함께 반환한다."""
    summary = run(query, use_tool=use_tool)
    history = load_history(history_path)
    history.append(summary)
    save_history(history, history_path)
    return {
        "last": summary,
        "history_summary": summarize_history(history),
    }


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Rapid: Solar Pro 4 기반 도로 구간 전환 작업 생성 에이전트"
    )
    parser.add_argument("query", nargs="?", default=None, help="사용자 요청 텍스트")
    parser.add_argument("--no-tool", action="store_true", help="도구 호출 없이 구조화 출력만 사용")
    parser.add_argument("--history", default="rapid_history.json", help="히스토리 파일 경로")
    parser.add_argument("--pretty", action="store_true", help="JSON 결과를 보기 좋게 출력")
    args = parser.parse_args()

    query = args.query
    if query is None:
        query = sys.stdin.read().strip()
    if not query:
        print("사용법: rapid.py '사용자 요청'  또는  echo '요청' | rapid.py", file=sys.stderr)
        sys.exit(2)

    result = run_and_record(
        query,
        use_tool=not args.no_tool,
        history_path=args.history,
    )

    out = json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None)
    print(out)
