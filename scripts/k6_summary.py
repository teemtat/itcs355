"""Print one line per k6 --summary-export file: throughput, p50/p95/p99, errors.

    python scripts/k6_summary.py reports/lab3/k6/*.json
"""
import json
import sys

print(f"{'run':28} {'req/s':>7} {'pred/s':>7} {'p50':>6} {'p95':>6} {'p99':>6} {'err%':>6} "
      f"{'srv p95':>8} {'score p95':>9}")
for path in sys.argv[1:]:
    m = json.load(open(path))["metrics"]
    lat, app, score = m["predict_latency_ms"], m.get("server_app_ms", {}), m.get("server_score_ms", {})
    print(f"{path.rsplit('/', 1)[-1].removesuffix('.json'):28} {m['http_reqs']['rate']:7.1f} "
          f"{m.get('predictions', {}).get('rate', 0):7.1f} {lat['med']:6.0f} {lat['p(95)']:6.0f} "
          f"{lat['p(99)']:6.0f} {100 * m['predict_failures']['value']:6.2f} "
          f"{app.get('p(95)', float('nan')):8.0f} {score.get('p(95)', float('nan')):9.0f}")
