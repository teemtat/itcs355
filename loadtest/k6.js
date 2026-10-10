// ITCS355 Lab 3 — load test.
//
//   k6 run -e TARGET=https://<service-url> -e TOKEN=<id-token> -e VUS=10 loadtest/k6.js
//   make loadtest                      # 1, 10, 50 VUs against the deployed service
//
// TARGET is the service BASE url; the path follows from MODE:
//   MODE=single (default)  POST /predict, one row
//   MODE=batch  ROWS=n     POST /predict/batch, n rows (schema caps n at 100)
// TOKEN is an identity token (the service is not public): `python scripts/serve_ctl.py token`.
//
// Results are committed in reports/lab3-load.md, raw summaries in reports/lab3/k6/.
// An uncommitted load test is not evidence.

import http from 'k6/http';
import { check } from 'k6';
import { Trend, Rate, Counter } from 'k6/metrics';

const latency = new Trend('predict_latency_ms', true);
const serverApp = new Trend('server_app_ms', true);      // server-side total
const serverScore = new Trend('server_score_ms', true);  // model.predict_proba only
const failures = new Rate('predict_failures');
const predictions = new Counter('predictions');          // rows scored, not requests

const MODE = __ENV.MODE || 'single';
const ROWS = Number(__ENV.ROWS || 100);

// LATENCY TARGET — stated 2026-09-26, committed BEFORE any measurement against the
// deployed endpoint (see the git log: this commit precedes every file in reports/lab3/k6/).
//
//   p95 < 200 ms for POST /predict (one row), at 10 concurrent clients,
//   measured from a client in Thailand to asia-southeast1, error rate < 1%.
//
// Why 200 ms: the consumer is an operator dashboard that scores a machine when its
// reading arrives; ~200 ms is the point past which a UI update stops feeling immediate.
// The budget includes the Thailand-Singapore round trip, because the user pays for it.
// Why concurrency 10: ~10 dashboards polling at once is the expected peak. The target
// applies to single-row calls; batch runs report the same thresholds but are judged
// per-row, not against 200 ms.
export const options = {
  vus: Number(__ENV.VUS || 10),
  duration: __ENV.DURATION || '60s',
  thresholds: {
    'predict_latency_ms': ['p(95)<200'],
    'predict_failures': ['rate<0.01'],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max', 'count'],
};

const base = {
  temp_c: 78.4,
  vibration_mm_s: 3.1,
  pressure_kpa: 315.2,
  hours_since_service: 4200,
  load_pct: 68.0,
  ambient_humidity: 55.0,
};

function row(i) {
  // Vary the values so nothing downstream can cache one answer.
  return { ...base, temp_c: 60 + (i * 7.3) % 35, load_pct: 20 + (i * 13.1) % 80 };
}

const path = MODE === 'batch' ? '/predict/batch' : '/predict';
const payload = MODE === 'batch'
  ? JSON.stringify({ rows: Array.from({ length: ROWS }, (_, i) => row(i)) })
  : JSON.stringify(base);
const rowsPerRequest = MODE === 'batch' ? ROWS : 1;

const headers = { 'Content-Type': 'application/json' };
if (__ENV.TOKEN) headers.Authorization = `Bearer ${__ENV.TOKEN}`;

function serverTiming(res, name) {
  const h = res.headers['Server-Timing'] || '';
  const m = h.match(new RegExp(`${name};dur=([0-9.]+)`));
  return m ? Number(m[1]) : null;
}

export default function () {
  const res = http.post(`${__ENV.TARGET}${path}`, payload, { headers });
  latency.add(res.timings.duration);
  failures.add(res.status !== 200);
  if (res.status === 200) {
    predictions.add(rowsPerRequest);
    const app = serverTiming(res, 'app');
    const score = serverTiming(res, 'score');
    if (app !== null) serverApp.add(app);
    if (score !== null) serverScore.add(score);
  }
  check(res, {
    'status is 200': (r) => r.status === 200,
    'version reported': (r) => r.headers['X-Model-Version'] !== undefined,
  });
}
