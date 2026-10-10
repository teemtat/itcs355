# Lab 4 — CI/CD, monitoring, and drift

I automated the path from a commit to a deployed staging service, then watched the
running service and broke it on purpose to check that the watching works.

## Detection time, predicted before injecting (written 2026-10-10 ~04:40 UTC)

Injection is planned for ~05:11 UTC: the replay switches from normal readings to
readings with temp_c +6 °C, at 1 request per second.

- The detector looks at the last 30 minutes (~1,800 readings) every 5 minutes.
- From `reports/lab4/calibration.json`, PSI on temp_c crosses 0.05 when about **35%** of
  the window is drifted (30% → 0.040, 40% → 0.062). 35% of 30 minutes is ~10.5 minutes.
- The next scheduled check after that is 0–5 minutes later, and Cloud Monitoring needs
  another ~1–3 minutes to evaluate the policy and send the email.

**Prediction: the alert arrives 12–19 minutes after injection.**

(The rest of this report is written after the exercise.)
