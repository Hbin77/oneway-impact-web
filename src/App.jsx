import { useEffect, useRef, useState, useCallback } from "react";
import "./style.css";

const POLL_MS = 2500;
const MAX_POLLS = 60;

export default function App() {
  const [place, setPlace] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [markdown, setMarkdown] = useState("");
  const [lat, setLat] = useState("");
  const [lng, setLng] = useState("");
  const [addr, setAddr] = useState("");
  const [mapError, setMapError] = useState("");

  const mapRef = useRef(null);

  useEffect(() => {
    const key = import.meta.env.VITE_GOOGLE_MAPS_JS_API_KEY;
    if (!key) {
      setMapError("지도 API 키가 설정돼 있지 않습니다.");
      return;
    }
    if (window.google?.maps) {
      initMap();
      return;
    }
    const script = document.createElement("script");
    script.src = `https://maps.googleapis.com/maps/api/js?key=${key}&callback=__gm_init`;
    script.async = true;
    script.defer = true;
    window.__gm_init = initMap;
    document.head.appendChild(script);

    return () => {
      if (script.parentNode) script.parentNode.removeChild(script);
      delete window.__gm_init;
    };
  }, []);

  function initMap() {
    if (!window.google?.maps) {
      setMapError("지도 API 로딩에 실패했습니다.");
      return;
    }
    const el = document.getElementById("map");
    if (!el) return;

    const map = new window.google.maps.Map(el, {
      center: { lat: 35.89, lng: 127.77 },
      zoom: 13,
      mapTypeControl: false,
      streetViewControl: false,
      fullscreenControl: false,
    });

    const marker = new window.google.maps.Marker({
      map,
      position: map.getCenter(),
      draggable: true,
    });

    marker.addListener("dragend", () => {
      const p = marker.getPosition();
      if (!p) return;
      setLat(p.lat().toFixed(6));
      setLng(p.lng().toFixed(6));
      reverseGeocode(p.lat(), p.lng());
    });

    map.addListener("click", (e) => {
      const p = e.latLng;
      if (!p) return;
      marker.setPosition(p);
      setLat(p.lat().toFixed(6));
      setLng(p.lng().toFixed(6));
      reverseGeocode(p.lat(), p.lng());
    });

    const c = map.getCenter();
    reverseGeocode(c.lat(), c.lng());
  }

  async function reverseGeocode(lat, lng) {
    try {
      const res = await fetch("/api/geocode", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lat, lng }),
      });
      const json = await res.json();
      if (json?.address) {
        setAddr(json.address);
        setPlace(json.address);
      }
    } catch (e) {
      setAddr("");
    }
  }

  const run = async () => {
    let target = place.trim();
    let body = {};

    if (lat && lng) {
      target = addr || `${lat}, ${lng}`;
      body = { place: target, lat: parseFloat(lat), lng: parseFloat(lng) };
    } else if (target) {
      body = { place: target };
    } else {
      setError("지명이나 지도 위치를 입력해 주세요.");
      return;
    }

    setLoading(true);
    setError("");
    setResult(null);
    setMarkdown("");
    setJobId(null);
    setJobStatus("queued");
    setPollCount(0);
    setPolling(true);

    try {
      const res = await fetch("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const text = await res.text().catch(() => "");
      let json = null;
      if (res.ok && text) {
        try { json = JSON.parse(text); } catch { json = null; }
      }

      if (!res.ok || !json) {
        setError(json?.error || `분석 요청이 실패했습니다(상태: ${res.status}).`);
        setLoading(false);
        setPolling(false);
        return;
      }

      if (json.job_id) {
        setJobId(json.job_id);
        setJobStatus("queued");
        setLoading(false);
        return;
      }

      if (json.markdown) {
        setResult(json);
        setMarkdown(json.markdown || "");
        setLoading(false);
        setPolling(false);
        return;
      }

      setError("예기치 않은 응답입니다.");
      setLoading(false);
      setPolling(false);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError("서버 연결 중 오류가 발생했습니다: " + msg);
      setLoading(false);
      setPolling(false);
    }
  };

  useEffect(() => {
    if (!polling || !jobId) return;
    const timer = setInterval(() => {
      let cancelled = false;
      fetch(`/api/result/${jobId}`)
        .then((r) => r.json())
        .then((json) => {
          if (cancelled) return;
          if (json?.status === "done") {
            setResult({
              place: json.place || "",
              bbox: json.bbox || [],
              data: {
                combination: (json.combination || []).map((r) => ({
                  round: Number(r.round),
                  name: r.name,
                  grade: r.grade,
                  cum: r.cum,
                })),
                braess: (json.braess || []).map((r) => ({
                  rank: Number(r.rank),
                  name: r.name,
                  grade: r.grade,
                  flow: r.flow,
                  delta: r.delta,
                  pct: r.pct,
                })),
                mustNot: (json.mustNot || []).map((r) => ({
                  rank: Number(r.rank),
                  name: r.name,
                  grade: r.grade,
                  flow: r.flow,
                  delta: r.delta,
                  pct: r.pct,
                })),
                biz: json.biz || null,
              },
              summary: {
                tsttHours: json.summary?.tsttHours
                  ? String(json.summary.tsttHours).replace(/,/g, "")
                  : "",
                savedHours: json.summary?.savedHours
                  ? String(json.summary.savedHours).replace(/,/g, "")
                  : "",
              },
              markdown: json.markdown || "",
            });
            setMarkdown(json.markdown || "");
            setJobStatus("done");
            setPolling(false);
            setPollCount(0);
            return;
          }
          if (json?.status === "error") {
            setError(json.error || "분석 중 오류가 발생했습니다.");
            setJobStatus("error");
            setPolling(false);
            setPollCount(0);
            return;
          }
          if (!json?.job_id && json?.status !== "done") {
            // job_id 없는 done 이외 응답은 더 이상 기다리지 않는다
            setJobStatus("running");
            setPolling(false);
            setError("분석 결과 확인을 중단했습니다. 다시 시도해 주세요.");
            return;
          }
          setJobStatus(json?.status || "running");
          setPollCount((prev) => (prev >= MAX_POLLS - 1 ? MAX_POLLS : prev + 1));
        })
        .catch(() => {
          if (!cancelled) {
            setError("결과 확인 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.");
            setPolling(false);
          }
        });
      return () => {
        cancelled = true;
      };
    }, POLL_MS);

    return () => clearInterval(timer);
  }, [polling, jobId]);

  useEffect(() => {
    if (jobStatus === "done") {
      return;
    }
    if (pollCount >= MAX_POLLS) {
      setError("분석이 예상보다 오래 걸리고 있습니다. 잠시 후 다시 시도해 주세요.");
      setPolling(false);
    }
  }, [pollCount, jobStatus]);

  const downloadMd = () => {
    if (!markdown) return;
    const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const safeName = (place || "결과")
      .slice(0, 40)
      .replace(/[^A-Za-z0-9가-힣 -]/g, "")
      .trim() || "결과";
    a.download = `일방통행-영향분석-${safeName}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="app">
      <header className="header">
        <h1>도로 일방통행 영향도 분석</h1>
        <p className="sub">지도에서 위치를 고르거나 지명을 입력하면 통행시간 개선안·위험 구간·상권 영향을 계산합니다.</p>
      </header>

      <section className="map-section">
        <div id="map" className="map"></div>
        {mapError && <div className="map-error">{mapError}</div>}
        <div className="map-info">
          <div className="map-coord">
            {lat && lng ? (
              <span>위도 {lat}° / 경도 {lng}°</span>
            ) : (
              <span className="muted">지도에서 위치를 선택하세요</span>
            )}
          </div>
          {addr && <div className="map-addr">선택 위치: {addr}</div>}
        </div>
      </section>

      <section className="input-card">
        <input
          className="input"
          placeholder="예: 순천 원도심, 대구 중구 동성로"
          value={place}
          onChange={(e) => setPlace(e.target.value)}
        />
        <button className="btn-primary" onClick={run} disabled={loading}>
          {loading ? "분석 중…" : "분석하기"}
        </button>
        {error && <div className="error">{error}</div>}
      </section>

      {(result ||
        jobStatus === "queued" ||
        jobStatus === "running" ||
        jobStatus === "error") ? (
        <section className="results">
          {(jobStatus === "queued" || jobStatus === "running") && (
            <div className="status-wait">
              {jobStatus === "queued"
                ? "분석 요청을 보냈습니다. 결과를 기다리는 중..."
                : "분석 중(약 1~3분)…"}
            </div>
          )}
          {jobStatus === "error" && (
            <div className="error">분석 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.</div>
          )}
          {result && (
            <>
              <div className="result-head">
                <h2>{result.place}</h2>
                <span className="meta">
                  위경도 {result.bbox?.[0]}°, {result.bbox?.[1]}° / {result.bbox?.[2]}°, {result.bbox?.[3]}°
                </span>
              </div>

              {result.data?.combination && result.data.combination.length > 0 && (
                <div className="card">
                  <h3>★ 다구간 조합 최적안</h3>
                  <p className="card-desc">
                    한 구간씩 최선을 고르고 확정하기를 반복했다. 구간 하나를 바꾸면 교통이 재배분되어
                    나머지 구간의 값이 전부 달라지므로, 매 라운드마다 바뀐 도로망 위에서 다시 계산했다.
                  </p>
                  <table className="table">
                    <thead>
                      <tr>
                        <th>라운드</th>
                        <th>전환 구간</th>
                        <th>등급</th>
                        <th>누적 개선율</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.data.combination.map((r, i) => (
                        <tr key={i}>
                          <td>{r.round}</td>
                          <td>{r.name}</td>
                          <td>{r.grade}</td>
                          <td>{r.cum}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  <p className="body-save">
                    이 구역 운전자들이 첨두 1시간 동안 도로에서 보내는 시간의 합이{" "}
                    <strong>{result.summary?.tsttHours}시간</strong>이다.
                    위 전환으로 <strong>{result.summary?.savedHours}시간</strong>이 사라진다.
                    표지판과 노면표시 교체 외에 예산은 들지 않으며, 실제 교통체계개선(TSM) 사업의
                    통상 효과가 1~3%임을 감안하면 공사 없이 얻는 값이다.
                  </p>
                </div>
              )}

              {result.data?.braess?.length > 0 && (
                <div className="card">
                  <h3>① 일방통행으로 바꾸면 전체가 좋아지는 구간</h3>
                  <p className="card-desc">
                    도로를 막았는데 총 통행시간이 줄어드는 구간이다. 브라에스 역설에 해당하며,
                    예산 없이 표지판만 바꿔 얻는 개선이다.
                  </p>
                  <table className="table">
                    <thead>
                      <tr>
                        <th>순위</th>
                        <th>도로명</th>
                        <th>등급</th>
                        <th>현재 교통량</th>
                        <th>ΔTSTT</th>
                        <th>개선율</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.data.braess.map((r) => (
                        <tr key={r.rank}>
                          <td>{r.rank}</td>
                          <td>{r.name}</td>
                          <td>{r.grade}</td>
                          <td>{r.flow}</td>
                          <td>{r.delta}</td>
                          <td>{r.pct}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {result.data?.mustNot?.length > 0 && (
                <div className="card">
                  <h3>② 절대 막으면 안 되는 구간</h3>
                  <p className="card-desc">
                    <strong>구간과 방향까지 봐야 한다.</strong> 같은 도로명이라도 구간번호가 다르면
                    다른 구간이고, 방향이 다르면 반대 차선이다.
                  </p>
                  <table className="table">
                    <thead>
                      <tr>
                        <th>순위</th>
                        <th>도로명</th>
                        <th>등급</th>
                        <th>현재 교통량</th>
                        <th>ΔTSTT</th>
                        <th>악화율</th>
                      </tr>
                    </thead>
                    <tbody>
                      {result.data.mustNot.map((r) => (
                        <tr key={r.rank}>
                          <td>{r.rank}</td>
                          <td>{r.name}</td>
                          <td>{r.grade}</td>
                          <td>{r.flow}</td>
                          <td>{r.delta}</td>
                          <td>{r.pct}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {result.data?.biz && (
                <div className="card biz-card">
                  <h3>상권 영향</h3>
                  <p className="biz-line">
                    평균 접근시간 {result.data.biz.mean0}분 → {result.data.biz.mean1}분 ({result.data.biz.dmean})
                  </p>
                  <p className="biz-line">
                    <strong>최악 상가 {result.data.biz.worst}</strong>
                  </p>
                  <p className="biz-verdict">{result.data.biz.verdict}</p>
                </div>
              )}

              <div className="notice">
                <h4>해석 주의</h4>
                <ul>
                  <li>지오코딩: Google Maps Geocoding API (region=kr, language=ko)</li>
                  <li>도로망: OpenStreetMap (OSM). 차로수·제한속도는 국가표준노드링크(국토교통부, 2026-08) 실측값을 우선 쓰고, 매칭 실패 시 도로등급 기본값으로 보정한다</li>
                  <li>OD는 도로 연장 기반 합성값이다. 절대 시간이 아니라 구간 간 상대 비교로만 쓸 것</li>
                  <li>보행 안전·형평성은 반영되지 않았다. 통행시간과 상권 접근성 두 축만 본 결과다</li>
                  <li>조합 최적안은 탐욕적 국소탐색 결과이며 전역 최적해가 아니다</li>
                </ul>
              </div>

              <button className="btn-secondary" onClick={downloadMd}>
                마크다운 보고서 다운로드
              </button>
            </>
          )}
        </section>
      ) : null}
    </div>
  );
}
