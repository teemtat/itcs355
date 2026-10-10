# Post-mortem — temperature readings 6 °C too high (Lab 4 injected drift)

**What fired:**
Cloud Monitoring policy "itcs355 input drift: PSI above 0.05". The drift job's check at
05:25:15 UTC scored temp_c at PSI **0.0655** over the last 30 minutes (1,781 readings);
every other feature stayed at 0.011 or below. Incident opened and email sent at
**05:28 UTC**, 17 minutes after the injection at 05:10:59.

**True cause:**
A broken upstream reading, not a change in the machines. From 05:10:59 every temp_c
value was exactly 6.000 °C higher, on all 240 machines at the same second, with the
spread unchanged (std 9.87 → 9.63) and every other feature unchanged, including the
ones that move with real heat (vibration, hours since service). Real heating does not
start on every machine in the same second and leave everything else alone; a sensor
gateway or calibration change does. Schema, nulls and ranges all passed (85 °C is a
plausible temperature), which is why only the drift check saw it.

**Retrain, roll back, or no action — and why:**
Neither retrain nor roll back. Fix the producer and keep model v1 serving.
- **Retraining is the harmful option.** It would teach the model that 85 °C is normal
  for these machines. When the sensor is fixed, the same model would then under-score
  every machine by 6 °C, and that means missed failures. Retraining on corrupted data
  would replace the last good model with a worse one.
- **Rollback does nothing.** v1 is already the version serving, and any version
  mis-scores wrong inputs.
- **What to do:** correct or recalibrate the gateway. Then mark the readings from
  05:10:59 until the fix as bad, so the Lab 5 retrain skips them or uses them with
  6 °C subtracted.
- **What would change my mind:** an independent thermometer agreeing with the higher
  readings, plus failure rates rising over the following weeks. That would mean the
  machines really run hotter, and then retraining on the new data is the right call.

**What this would have cost if unnoticed for a week:**
The model would flag 12.7% of readings instead of 10.5% (`scripts/drift_calibrate.py`
and the v1 model on the stream). Only 1 in 3 of the newly flagged readings came from a
machine that was really failing.
- **Assumption:** one inspection decision per machine per day, from that machine's
  average risk, with an alarm above 0.3. On that basis, flagged machines go from 9 to 11.
- That is ~14 extra inspections a week, ~9–10 of them needless.
- **Assumption:** each needless inspection costs 1,500 THB (a technician hour plus an
  hour of stopped machine). That makes **~14,000 THB a week**.

The larger cost is the data. At 1 reading per second, that is ~600,000 wrong temperature
readings a week. If the 28-day retrain ran over them, the damage would last long after
the gateway was fixed.

**How to prevent or detect it faster:**
Add an ingestion check before the model, comparing each 5-minute batch with the one
before it: the fleet-wide temp_c median may not jump by more than 2 °C. A real fleet
cannot warm 6 °C in five minutes, so this catches the step within one batch. Right now
the 30-minute PSI window has to fill about half-way first. The PSI check stays in place
for slow changes that a step check misses. The `mix` mode (a different fleet mix) is
still caught by neither, which is a gap I am leaving open on purpose; see the report.
