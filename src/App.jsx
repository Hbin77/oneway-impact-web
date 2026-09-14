import { useState, useCallback, useRef } from "react";
import "./style.css";

const HOST = "https://" + window.location.hostname;
const API = HOST + ":9001";

const RESULT_CARDS = [
  {
    key: "head",
    title: "위치 확인",
    render: (r) => (
      <>
        {r.bbox ? (
          <div className="card-row">
            <span className="card-label">경계 상자</span>
            <span className="card-value">
              ({r.bbox[0].toFixed(5)}, {r.bbox[1].toFixed(5)}) ~ (
              {r.bbox[2].toFixed(5)}, {r.bbox[3].toFixed(5)})
            </span>
          </div>
        ) : null}
        {r.address ? (
          <div className="card-row">
            <span className="card-label">추정 주소</span>
            <span className="card-value">{r.address}</span>
          </div>
        ) : null}
        {r.place ? (
          <div className="card-row">
            <span className="card-label">검색어</span>
            <span className="card-value">{r.place}</span>
          </div>
        ) : null}
      </>
    ),
  },
  {
    key: "roadCount",
    title: "분석 대상 도로",
    render: (r) =>
      r.roadCount != null ? (
        <div className="card-row">
          <span className="card-label">도로 수</span>
          <span className="card-value">{r.roadCount}개</span>
        </div>
      ) : null,
  },
  {
    key: "blockCount",
    title: "통행 제한 블록",
    render: (r) =>
      r.blockCount != null ? (
        <div className="card-row">
          <span className="card-label">블록 수</span>
          <span className="card-value">{r.blockCount}개</span>
        </div>
      ) : null,
  },
  {
    key: "linkCount",
    title: "연결 교차로",
    render: (r) =>
      r.linkCount != null ? (
        <div className="card-row">
          <span className="card-label">교차로 수</span>
          <span className="card-value">{r.linkCount}개</span>
        </div>
      ) : null,
  },
  {
    key: "avgWidth",
    title: "평균 도로 폭",
    render: (r) =>
      r.avgWidth != null ? (
        <div className="card-row">
          <span className="card-label">평균 폭</span>
          <span className="card-value">{r.avgWidth.toFixed(1)}m</span>
        </div>
      ) : null,
  },
  {
    key: "totalLength",
    title: "총 도로 연장",
    render: (r) =>
      r.totalLength != null ? (
        <div className="card-row">
          <span className="card-label">총 연장</span>
          <span className="card-value">{r.totalLength.toFixed(0)}m</span>
        </div>
      ) : null,
  },
  {
    key: "onewayRatio",
    title: "일방통행 전환 비율",
    render: (r) =>
      r.onewayRatio != null ? (
        <div className="card-row">
          <span className="card-label">전환 대상 비율</span>
          <span className="card-value">
            {r.onewayRatio.toFixed(1)}% (도로 수 기준)
          </span>
        </div>
      ) : null,
  },
];

const RESULT_COUNTS = [
  "bidirectional",
  "oneway",
  "twoWayConfusion",
  "total",
];

function formatCount(v) {
  if (v == null) return "—";
  if (typeof v === "number") return v.toLocaleString();
  return String(v);
}

/*
 * DesignsBlock - 구역 내 기존 일방통행 지정 현황을 접이식으로 표시
 * items 필드명: roadNm, appnResn, appnYear, roadBt, roadEt, cartrkCo, mdstrpYn
 */
function DesignationsBlock({ data }) {
  const d = data || {};
  const count = parseInt(d.count, 10) || 0;
  const items =
    Array.isArray(d.items) && d.items.length > 0 ? d.items : [];
  const [open, setOpen] = useState(false);

  if (count === 0) {
    return (
      <div className="designations">
        <h3>구역 내 기존 공식 일방통행 지정</h3>
        <p>해당 구역에 등록된 공식 지정 없음</p>
      </div>
    );
  }

  const rows = items.map((it) => ({
    name: (it.roadNm || it.name || it.roadName || "—").trim(),
    width:
      it.roadBt != null ? Number(it.roadBt).toFixed(1) : null,
    length:
      it.roadEt != null ? Number(it.roadEt).toFixed(1) : null,
    year:
      it.appnYear != null
        ? parseInt(it.appnYear, 10)
        : null,
  }));

  return (
    <div className="designations">
      <h3>
        구역 내 기존 공식 일방통행 지정
        <span className="badge">{count}건</span>
      </h3>
      {open && (
        <div className="designations-body">
          <div className="designations-list">
            {rows.map((r, i) => (
              <div key={i} className="designations-item">
                <div className="designations-row">
                  <span className="designations-name">{r.name}</span>
                  {r.width || r.length ? (
                    <span className="designations-meta">
                      {r.width ? "폭 " + r.width + "m" : ""}
                      {r.width && r.length ? " · " : ""}
                      {r.length ? "연장 " + r.length + "m" : ""}
                    </span>
                  ) : null}
                </div>
                {r.year ? (
                  <div className="designations-meta designations-year">
                    지정 연도 {r.year}
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        </div>
      )}
      {count > 0 && (
        <button
          className="designations-toggle"
          onClick={() => setOpen(!open)}
        >
          {open ? "접기" : "상세 보기"}
        </button>
      )}
    </div>
  );
}

export default function App() {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("idle");
  const [result, setResult] = useState(null);
  const [agentResult, setAgentResult] = useState(null);
  const [onewayDesignations, setOnewayDesignations] =
    useState(null);
  const mountedRef = useRef(true);
  const queryTimeoutRef = useRef(null);

  const fetchGitHubRepos = useCallback(async () => {
    try {
      const res = await fetch(
        "https://api.github.com/repos/hbinserver/oneway-impact-web",
        { mode: "cors" }
      );
      if (!res.ok) {
        console.warn(
          "GitHub API status:",
          res.status,
          "Public repo availability:"
        );
        return;
      }
      const data = await res.json();
    } catch (e) {
      console.warn(
        "GitHub API 조회 불가:",
        e.message
      );
    }
  }, []);

  const doAnalyze = useCallback(
    async (place) => {
      const trimmed = place.trim();
      if (!trimmed) {
        return;
      }

      setStatus("loading");
      setResult(null);
      setAgentResult(null);
      setOnewayDesignations(null);

      try {
        const res = await fetch(API + "/api/analyze", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ place: trimmed }),
          signal: aborter.ref.current?.signal,
        });

        if (!res.ok) {
          const errBody = await res
            .json()
            .catch(() => ({}));
          throw new Error(
            errBody.error || `HTTP ${res.status}`
          );
        }

        const data = await res.json();
        setResult(data);
        setStatus("done");

        /*
         * 에이전트 요약은 결과 수신 직후 별도 요청으로 가져온다.
         * 빠른 피드백을 위해 결과 렌더링을 막지 않는다.
         */
        setStatus("agent");
        try {
          const agentRes = await fetch(
            API + "/api/agent",
            {
              method: "POST",
              headers: {
                "Content-Type": "application/json"
              },
              body: JSON.stringify({
                job_id: data.job_id,
                place: trimmed,
                oneway_designations:
                  data.oneway_designations,
              }),
            }
          );
          if (agentRes.ok) {
            const agentData = await agentRes.json();
            setAgentResult(agentData);
            setOnewayDesignations(
              agentData.oneway_designations
            );
          }
        } catch (agentErr) {
          console.warn(
            "에이전트 요약 조회 실패:",
            agentErr.message
          );
        }
      } catch (err) {
        setResult({
          error:
            err instanceof Error
              ? err.message
              : String(err),
        });
        setStatus("error");
      }
    },
    []
  );

  const handleSubmit = useCallback(
    (e) => {
      e.preventDefault();
      if (queryTimeoutRef.current) {
        clearTimeout(queryTimeoutRef.current);
        queryTimeoutRef.current = null;
      }
      doAnalyze(query);
    },
    [query, doAnalyze]
  );

  const handleChange = useCallback(
    (e) => {
      const v = e.target.value;
      setQuery(v);
      if (queryTimeoutRef.current) {
        clearTimeout(queryTimeoutRef.current);
      }
      queryTimeoutRef.current = setTimeout(() => {
        doAnalyze(v);
      }, 800);
    },
    [doAnalyze]
  );

  const handleAbort = useCallback(() => {
    aborter.abort();
  }, []);

  const reset = useCallback(() => {
    setStatus("idle");
    setResult(null);
    setAgentResult(null);
    setOnewayDesignations(null);
    setQuery("");
  }, []);

  const fmt = (v, fallback = "—") => {
    if (v == null) return fallback;
    if (typeof v === "number") return v.toFixed(1);
    return String(v);
  };

  const downloadReport = useCallback(() => {
    const enc = new TextEncoder();
    const uint8 = enc.encode(result.markdown || "");
    const blob = new Blob([uint8], {
      type: "text/markdown;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download =
      "oneway-impact-report.md";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }, [result]);

  return (
    <div className="app">
      <header className="app-header">
        <h1 className="app-title">
          전국일방통행도로표준데이터 기반
          <br />
          일방통행 전환 영향 분석
        </h1>
        <p className="app-subtitle">
          경찰청 전국일방통행도로표준데이터(공공데이터포털)를
          기반으로, 지정 구역 내 도로 네트워크의 일방통행
          전환 가능성과 교통 영향을 신속하게 분석한다.
        </p>
      </header>

      <main className="app-main">
        <section className="input-section">
          <form
            className="input-form"
            onSubmit={handleSubmit}
          >
            <div className="input-row">
              <input
                className="input-field"
                type="text"
                value={query}
                onChange={handleChange}
                placeholder="분석할 구역명을 입력하세요 (예: 순천시 원도심, 서울 종로구)"
                disabled={
                  status === "loading" ||
                  status === "agent"
                }
                autoComplete="off"
                autoFocus
              />
              <div className="input-actions">
                {status === "loading" ||
                status === "agent" ? (
                  <button
                    className="btn btn-secondary"
                    type="button"
                    onClick={handleAbort}
                  >
                    취소
                  </button>
                ) : null}
                <button
                  className="btn btn-primary"
                  type="submit"
                  disabled={
                    status === "loading" ||
                    status === "agent" ||
                    !query.trim()
                  }
                >
                  분석
                </button>
              </div>
            </div>
          </form>

          {status === "loading" && (
            <div className="status-bar status-loading">
              <span className="status-spinner" />
              <span className="status-text">
                분석 중입니다...
              </span>
            </div>
          )}

          {status === "agent" && (
            <div className="status-bar status-agent">
              <span className="status-spinner" />
              <span className="status-text">
                분석 완료 — 보고서 작성 중...
              </span>
            </div>
          )}

          {status === "error" && result?.error && (
            <div className="status-bar status-error">
              <span className="status-icon">!</span>
              <span className="status-text">
                {result.error}
              </span>
            </div>
          )}

          {status === "done" &&
            !agentResult &&
            result?.job_id && (
              <div className="status-bar status-done">
                <span className="status-icon">✓</span>
                <span className="status-text">
                  분석 완료.
                </span>
              </div>
            )}
        </section>

        {result && status === "done" && (
          <section className="result-section">
            <div className="result-head">
              <h2>분석 결과</h2>
              <div className="result-actions">
                {result.job_id && (
                  <span className="result-job-id">
                    작업 ID: {result.job_id}
                  </span>
                )}
                {result.markdown && (
                  <button
                    className="btn btn-secondary"
                    onClick={downloadReport}
                  >
                    보고서 다운로드 (.md)
                  </button>
                )}
              </div>
            </div>

            <DesignationsBlock
              data={onewayDesignations}
            />

            {result.error ? (
              <div className="result-error">
                <p>{result.error}</p>
              </div>
            ) : (
              <div className="result-cards">
                {RESULT_CARDS.map((card) => {
                  const val = result[card.key];
                  if (val == null) return null;
                  return (
                    <div key={card.key} className="card">
                      <div className="card-title">
                        {card.title}
                      </div>
                      <div className="card-body">
                        {card.render(result)}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}

            {result.bbox && (
              <div className="result-map">
                <div className="map-placeholder">
                  <div className="map-placeholder-icon">
                    ◯
                  </div>
                  <p>
                    지도 영역 ({result.bbox[0].toFixed(3)},
                    {result.bbox[1].toFixed(3)}) ~
                    ({result.bbox[2].toFixed(3)},
                    {result.bbox[3].toFixed(3)})
                  </p>
                  <p className="map-note">
                    실제 지도는 leaflets/mapbox 등
                    외부 지도 서비스로 대체할 수
                    있다.
                  </p>
                </div>
              </div>
            )}

            {result.roadNetworkSummary && (
              <div className="result-network">
                <h3>도로 네트워크 요약</h3>
                <pre className="network-pre">
                  {result.roadNetworkSummary}
                </pre>
              </div>
            )}

            <div className="result-foot">
              <p className="result-foot-note">
                출처: 경찰청 전국일방통행도로표준데이터
                (공공데이터포털, CC BY), 분석 엔진
                계산. 지도의 도로·교차로 정보는 분석
                엔진 내부 네트워크 표출 결과에
                기초한다.
              </p>
            </div>
          </section>
        )}

        {agentResult && (
          <section className="agent-section">
            <div className="agent-head">
              <h2>분석 요약 (에이전트)</h2>
              <div className="agent-meta">
                <span>
                  출처: 경찰청 전국일방통행도로표준데이터
                  (공공데이터포털, CC BY) + 분석
                  엔진 계산
                </span>
                <span>작업 ID: {agentResult.job_id}</span>
                {agentResult.oneway_designations && (
                  <span className="agent-designations-link">
                    구역 내 기존 지정{" "}
                    {(
                      agentResult.oneway_designations
                        .count || 0
                    ).toLocaleString()}
                    건
                  </span>
                )}
              </div>
            </div>

            {agentResult.reply_error ? (
              <div className="agent-error">
                <p>{agentResult.reply_error}</p>
              </div>
            ) : agentResult.reply ? (
              <div className="agent-body">
                <div
                  className="agent-markdown"
                  dangerouslySetInnerHTML={{
                    __html:
                      renderMarkdownSafe(
                        agentResult.reply
                      ),
                  }}
                />
              </div>
            ) : null}

            <DesignationsBlock
              data={
                agentResult.oneway_designations ||
                onewayDesignations
              }
            />

            <div className="agent-foot">
              <p className="agent-foot-note">
                출처: 경찰청 전국일방통행도로표준데이터
                (공공데이터포털, CC BY).
                에이전트 요약은 분석 엔진 수치를
                바탕으로 생성되며, 공식 지정 현황
                데이터와 구분하여 표시한다.
              </p>
            </div>
          </section>
        )}

        {status === "idle" && (
          <section className="idle-section">
            <div className="idle-card">
              <h2>분석 시작하기</h2>
              <p>
                분석을 원하는 구역의 이름이나 동·읍·면
                단위 지명을 입력하면, 해당 구역의 도로
                네트워크와 일방통행 지정 현황을 분석해
                결과를 제공한다.
              </p>
              <div className="idle-examples">
                <h3>예시</h3>
                <ul>
                  <li>
                    <strong>순천시 원도심</strong>
                    <span className="idle-example-note">
                      전라남도 순천시 인근 도로망
                    </span>
                  </li>
                  <li>
                    <strong>서울 종로구</strong>
                    <span className="idle-example-note">
                      서울특별시 종로구 인근 도로망
                    </span>
                  </li>
                  <li>
                    <strong>대구 수성구</strong>
                    <span className="idle-example-note">
                      대구 광역시 수성구 인근 도로망
                    </span>
                  </li>
                </ul>
              </div>
              <p className="idle-note">
                분석 결과는 참고용이며, 실제 일방통행
                지정·전환은 해당 지방자치단체의 교통
                계획과 예산에 따른다.
              </p>
            </div>
          </section>
        )}
      </main>

      <footer className="app-footer">
        <p>
          국토교통부·경찰청 전국일방통행도로표준데이터
          기반 분석 도구 (공공데이터포털, CC BY).
        </p>
        <p>
          엔진 계산치는 해당 구역 도로 네트워크
          분석에 기초하며, 공식 지정 현황과 다를 수
          있다.
        </p>
      </footer>
    </div>
  );
}

/* ---------- 아래 헬퍼는 기존 코드 유지 ---------- */

function renderMarkdownSafe(md) {
  /* 간단한 마크다운→HTML 변환 (요약 표시용) */
  if (!md) return "";
  let html = md
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/^### (.*)$/gm, "<h3>$1</h3>")
    .replace(/^## (.*)$/gm, "<h2>$1</h2>")
    .replace(/^# (.*)$/gm, "<h1>$1</h1>")
    .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.*?)\*/g, "<em>$1</em>")
    .replace(/`(.*?)`/g, "<code>$1</code>")
    .replace(/^• (.*)$/gm, "<li>$1</li>")
    .replace(/(<li>.*<\/li>)/s, "<ul>$1</ul>")
    .replace(/\n\n/g, "</p><p>")
    .replace(/\n/g, "<br />");
  return "<p>" + html + "</p>";
}

const aborter = {
  ref: { current: null },
  abort() {
    if (this.ref.current) {
      this.ref.current.abort();
      this.ref.current = null;
    }
  },
  create() {
    this.abort();
    this.ref.current = new AbortController();
    return this.ref.current.signal;
  },
};
