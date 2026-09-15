# PRD — oneway-impact-web (제출본)

## 1. 한 줄 정의

지자체 교통 담당자가 지명만 입력하면, 용역 발주 전에 일방통행 전환 후보 구간·통행시간 영향·상권 영향을 근거와 함께 빠르게 1차 검토하는 웹 서비스.

**누가 왜 쓰는지**  
- 지자체 교통과·도로과·도시계획 실무자(B2G). 용역 발주 전 자체 스크리닝 용도.  
- 결과를 보고서·민원 답변·회의 자료에 바로 붙일 수 있어야 한다.  
- 공유 링크로 바로 꺼내 쓰는 흐름이 자연스럽다.

## 2. 스킬 매핑

- 사용 스킬: `oneway-impact-analyzer` (원본: `scripts/road_impact.py` — Overpass 도로망 수집 + BPR + Frank-Wolfe UE 배정 + 방향 차단 재배정 + 조합 탐색 + 상권 영향 평가).  
- 엔진 `api/road_impact.py`는 예선 원본과 sha256 바이트 동일(수정 0). 서비스는 재구현 없이 원본 스크립트를 그대로 호출한다.  
- 서비스가 덧붙인 것: 비동기 job API, Google 지오코딩, 경찰청 전국일방통행도로표준데이터 컨텍스트, Solar Pro 4 에이전트(구조화 출력, 근거 인용, engine job_id 표시), A/B Street 미시 시뮬레이션 교차검증(abst_compare), React 화면.  
- 스킬의 "결과를 말할 때 반드시 붙일 것" 고지가 Solar 시스템 프롬프트 규칙으로 옮겨졌다. Solar는 엔진 산출물(마크다운)을 읽고 설명·구조화만 하며, 수치를 새로 만들거나 바꾸지 않는다.

## 3. 구현 범위

| 구분 | 내용 |
| --- | --- |
| P0 완료 | 지명 기반 분석 실행(Google 지오코딩 → bbox → 원본 스킬 실행) / 조합 최적안 / 브라에스 역설 구간 표 / 절대 막으면 안 되는 구간 표 / 상권 영향 / Solar 에이전트 질의응답(구조화, 근거 인용) / A/B Street 미시 시뮬레이션 교차검증(순천 원도심) / 배포(본인 서버 + Cloudflare Tunnel) |
| P1 향후 | 다른 지역 A/B Street 지도 / 3D 전/후 시각화 / Solar 도구 호출형 에이전트(스킬처럼 스스로 분석 실행·예외 재실행) |

## 4. 아키텍처

프론트 React(client) ↔ Vercel/서버 Python `/api`(Flask + gunicorn) ↔ 원본 `road_impact.py`(그대로 호출) + Google Geocoding + data.go.kr 경찰청 데이터 + Upstage Solar Pro 4 + A/B Street headless 서버. 모든 키는 서버 환경변수만, 클라이언트 소스·응답 JSON 노출 금지. 도로망은 OSM(Overpass, 미러 폴백) + 국가표준노드링크 차로수·제한속도.

**API 5개**  
- `POST /api/analyze` — 지명/위경도로 분석 요청, job_id 반환  
- `GET /api/result/<id>` — 결과 polling(done 시 결과 + abst_compare 첨부)  
- `POST /api/agent` — Solar 질문 요청, agent_job_id 반환  
- `GET /api/agent/<id>` — 에이전트 결과 polling(done 시 reply + oneway_designations + abst_compare)  
- `POST /api/geocode` — 위경도 → 주소(역지오코딩)

## 5. 검증 수치

- 순천 원도심 전체 흐름(엔진+지정현황+A/B Street+Solar): 176초(엔진 단독 약 90초, A/B Street 비교 13초).  
- 대구 3km bbox(Overpass 실시간, 로컬): 7분 26초 → "다른 지역 최대 8분".  
- 엔진 조합 최적안(순천): 3구간 전환 시 총통행시간 -1.079%(316,560→313,145 대·분), 매일 56.9시간 절감, 상권 평균 접근 6.34→6.30분, 최악 상가 +0.45분.  
- Solar 인용: 지정 현황 질문 시 경찰청 데이터 인용 공개 3/3회.  
- A/B Street 교차검증(무작위 수요): Drive 6,147건, 평균 357.3→358.4초(+0.3%), p90 547.2→549.5초, 전환 도로 통과량 1,136→306 — 무작위 수요 기반, 절대값 비교 금지, 방향성 교차검증.

## 6. 필요 설정

서버(/api) 환경변수만. 값 금지.

| 키 | 용도 | 발급처 |
| --- | --- | --- |
| `GOOGLE_MAPS_GEOCODING_API_KEY` | 지명 → 위경도 지오코딩(서버만, `region=kr`) | Google Maps Geocoding API (결제 카드·과금 필요) |
| `UPSTAGE_API_KEY` | Solar Pro 4 chat completions 호출(서버만) | Upstage 콘솔 (`api.upstage.ai/v1/chat/completions`, `upstage/solar-pro4`) |
| `DATA_GO_KR_KEY` | 경찰청 전국일방통행도로표준데이터 조회(서버만) | 공공데이터포털(data.go.kr 15028199) |
| `ABST_API` | A/B Street headless 서버 베이스 URL(예: `http://127.0.0.1:1234`, 서버만) | A/B Street headless 빌드 |

## 7. 한계·고지

- OD 통행량은 합성값 → 절대 시간이 아니라 구간 간 상대 비교로만 유효.  
- A/B Street 결과는 순천 원도심만 지원, 무작위 수요 기반 → 절대값 비교 금지, 방향성 교차검증용.  
- 조합 최적안은 탐욕적 국소탐색 결과, 전역 최적해가 아님.  
- 보행 안전·형평성은 계산 범위에 없음(통행시간·상권 접근성 두 축만).  
- Overpass 미러 4곳 전부 실패 시 분석 중단(순천 외 구역은 OSM 값 + 도로등급 기본값으로 계산).  
- Google Geocoding은 결제 카드 등록·과금 필요, 배포·운영 비용 본인 부담.  
- Vercel 서버리스 함수 1요청 최대 5분, 월 4 CPU-시간 범위 내 운영.

## 배포·소스

- 서비스: https://oneway.hbinserver.cloud  
- GitHub: https://github.com/Hbin77/oneway-impact-web  
- 커밋 해시: 제출 시 최종 해시 기입
