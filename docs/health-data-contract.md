# Health data contract: `wizard.health-data/1`

A **health-data connector** is an app that holds the owner's sign-in to one wearable or health service (WHOOP,
Oura, Garmin, Apple Health...) and offers that data, read-only and normalized, to other apps on the same box.
A **health front end** (the Health app) reads any number of connectors through this contract and never talks to a
provider itself. This mirrors the money stack: Money reads `wizard.bank-connection/1` from SimpleFIN, Monarch or
Teller; Health reads `wizard.health-data/1` from WHOOP and any later connector.

The same file lives in every connector repo and the front end (`wizard-whoop/docs/`, `wizard-fitbit/docs/`,
`wizard-health/docs/`). Change them together.

## Discovery

- A connector declares `provides: [health-data, <provider>-health-data]` in `wizard.md`.
- A front end declares `uses: [health-data]`. The App Manager reads that as the `health-data` spot, links the
  front end's network to **every** installed app that provides it, and writes the endpoint it granted into the
  front end's `/wizard/grants.json` (`provider_endpoints`).
- The front end probes each candidate's `GET /api/status`. A reply with `"contract": "wizard.health-data/1"` is a
  source; the reply names itself (`source.id`, `source.name`, `source.app_path`), so a new connector needs no code
  change in the front end. Candidates come from, in order: `grants.json` endpoints, the `HEALTH_SOURCES` env
  (`id=http://host:port,...`), and the built-in list of known connectors (as Money keeps for bank connections).
  An unreachable candidate is simply "not installed".
- The App Manager writes one endpoint per spot grant. When two connectors fill the spot, the front end still
  reaches both (both networks are linked) as long as each is in the built-in list or `HEALTH_SOURCES`.

## Transport and safety

- Plain HTTP on the connector's own port, JSON only, `GET` only for reads.
- Reads are **box-only**: the connector refuses contract reads that arrive through the private-access proxy (any
  `X-Forwarded-For`, `Forwarded`, `X-Real-IP`, `Tailscale-User-Login` or `CF-Connecting-IP` header). The front end
  calls the connector from its own container (`http://<connector-id>:<port>`), scripts call `127.0.0.1:<port>`.
- No reply ever contains keys, tokens, passwords or sign-in codes.
- Errors: `{"error": "<plain sentence>"}` with 400 (bad query), 403 (not box-only), 409 (not connected).

## `GET /api/status`

```json
{
  "contract": "wizard.health-data/1",
  "source": {"id": "wizard-whoop", "provider": "whoop", "name": "WHOOP", "app_path": "/wizard-whoop/"},
  "status": "connected",
  "account": {"name": "Sam"},
  "dataset": "4f0c9a1e2b7d...",
  "last_sync": "2026-10-31T01:02:09.000Z",
  "next_sync": "2026-10-31T01:22:09.000Z",
  "sync_every_minutes": 20,
  "history_from": "2026-03-26",
  "latest_date": "2026-10-31",
  "counts": {"days": 217, "sleeps": 254, "workouts": 122},
  "error": null,
  "scales": {
    "readiness": {"label": "Recovery", "min": 0, "max": 100},
    "sleep_score": {"label": "Sleep performance", "min": 0, "max": 100},
    "load": {"label": "Strain", "min": 0, "max": 21}
  }
}
```

- `status`: `not_configured` (no keys/sign-in yet), `needs_login` (provider ended the sign-in), `syncing`,
  `connected`, `rate_limited` (provider asked to slow down; data still readable). Only `connected`, `syncing`
  and `rate_limited` mean data is flowing.
- `dataset`: an opaque id of the connector's local copy. It changes when the copy is wiped (Disconnect, a new
  account). A front end that sees a different `dataset` drops what it holds from that source and re-reads.
- `scales` tells the front end what to call things and how to draw them. A front end shows `load.label`
  (WHOOP "Strain", Garmin "Training load", Oura "Activity") and never assumes a provider's scale.
- A connector may add its own fields (the WHOOP app adds its setup state); front ends ignore unknown fields.

### Sources without a readiness score (additive, since Fitbit 0.1.0)

Some providers have no recovery/readiness score in their API (Fitbit through Google's Health API: Daily Readiness
is not offered). Such a source says so in `scales` and never invents one:

```json
"scales": {
  "readiness": {"label": "Readiness", "min": 0, "max": 100, "available": false},
  "sleep_score": {"label": "Sleep score", "min": 0, "max": 100, "available": false},
  "load": {"label": "Active Zone Minutes", "short_label": "AZM", "unit": "min", "min": 0, "max": null,
           "scale_max": 60, "daily_goal": 22,
           "targets": {"high": [22, 45], "medium": [10, 22], "low": [0, 10]}, "over_margin": 15}
},
"provides": {"readiness": false, "sleep_score": false, "sleep_need": false, "hrv": true, "resting_hr": true,
             "load": "active_zone_minutes"}
```

- `scales.readiness.available: false` (and `provides.readiness: false`): every day has `readiness: null`. A front
  end shows recovery as **"no score"** for this source (grey), may still show `hrv_ms` and `resting_hr_bpm`, and
  must not derive a readiness number from them. Activity matched against recovery is then "no recovery data".
- Missing `available` means `true` (WHOOP and earlier connectors).

### Load scales other than WHOOP's 0-21 (additive)

`load` is always on the provider's own scale. Two optional fields let a front end classify any scale:

- `scale_max`: the top of the drawing/intensity scale. Use it when `max` is `null` (an open-ended scale such as
  Active Zone Minutes) to place a value on a 0..1 ramp: `min(value / scale_max, 1)`. When absent, use `max`.
- `targets`: per recovery band (`high`/`medium`/`low`, matching `readiness.band`), the `[low, high]` range of load
  that counts as the right amount for that day, on the source's own scale. `over_margin` is how far above the top
  of the range is still "a bit much" (beyond it: "too much"). Under the range is never red.
- When `targets` is absent, a front end uses WHOOP's 0-21 strain targets (Ready 14-18, So-so 10-14, Run down under
  10, `over_margin` 3), which is what `max: 21` sources mean.
- Day and workout `load` objects repeat `scale_max` (and `unit` when the scale has one) so a single record is
  self-describing: `{"value": 34, "max": null, "label": "Active Zone Minutes", "scale_max": 60, "unit": "min"}`.

Fitbit's numbers: Active Zone Minutes earn 1 per minute in the fat-burn heart-rate zone and 2 per minute in cardio
or peak; Fitbit's default daily goal is 22 (about 150 a week, the WHO/AHA activity guidance). The targets keep the
shape of WHOOP's: Ready = meet the goal up to about double (22-45), So-so = a light day up to the goal (10-22), Run
down = under 10, and "too much" at more than 15 above the band's top. `scale_max` 60 is "a very full day".

## `GET /api/days`

Normalized daily records, oldest first, with paging.

Query (all optional):

| name | meaning |
|---|---|
| `from`, `to` | `YYYY-MM-DD`, inclusive, on the record's `date` |
| `changed_since` | a `cursor` from an earlier reply; only days whose data changed after it. Empty string = everything |
| `limit` | days per page, default 100, max 500 |
| `page` | the `next_page` from the previous reply (keep the other query values the same) |

Reply:

```json
{
  "contract": "wizard.health-data/1",
  "source": "wizard-whoop",
  "dataset": "4f0c9a1e2b7d...",
  "days": [ /* day records */ ],
  "next_page": "eyJk...",
  "cursor": "w1.2840"
}
```

- `next_page` is `null` on the last page.
- `cursor` is the connector's change mark at the time of the **first** page of this read (every page repeats
  it). Store it only after the last page, then pass it as `changed_since` next time. Cursors are opaque strings.
- A day is returned again whenever anything in it changes (late scoring, a re-scored sleep, a new workout).
  The day record is always complete, so the front end replaces its copy of that `(source, date)`.

### Day record

```json
{
  "date": "2026-10-30",
  "updated_at": "2026-10-30T23:10:41.000Z",
  "readiness": {"score": 72, "band": "high", "state": "scored", "calibrating": false},
  "hrv_ms": 64.2,
  "resting_hr_bpm": 52,
  "spo2_pct": 96.1,
  "skin_temp_c": 33.6,
  "skin_temp_delta_c": null,
  "respiratory_rate": 15.4,
  "sleep": {
    "start": "2026-10-29T16:12:03.000Z", "end": "2026-10-30T00:01:10.000Z",
    "asleep_h": 7.02, "in_bed_h": 7.82, "awake_h": 0.8,
    "light_h": 3.6, "deep_h": 1.7, "rem_h": 1.72,
    "score": 81, "efficiency_pct": 90, "respiratory_rate": 15.4, "need_h": 8.4,
    "naps": [ {"start": "...", "end": "...", "asleep_h": 0.4} ]
  },
  "load": {"value": 12.4, "max": 21, "label": "Strain"},
  "calories_kcal": 2410,
  "energy_kj": 10083,
  "avg_hr_bpm": 68,
  "max_hr_bpm": 171,
  "steps": null,
  "workouts": [
    {"id": "b2f...", "sport": "Running", "start": "...", "end": "...", "minutes": 42,
     "load": {"value": 9.8, "max": 21, "label": "Strain"},
     "avg_hr_bpm": 148, "max_hr_bpm": 176, "calories_kcal": 512, "energy_kj": 2142, "distance_m": 7210,
     "zones_min": [0.5, 3.2, 10.1, 18.4, 9.1, 0.7]}
  ],
  "extras": {"whoop": {"cycle_id": "1234", "sleep_consistency_pct": 77, "disturbances": 9}}
}
```

Rules:

- `date` is the owner's local calendar day the record belongs to (the day he woke up / lived through), as the
  provider defines it.
- Any value the provider did not give is `null`; a section with nothing at all is `null` (`readiness`, `sleep`,
  `load`). Never invent a value.
- `readiness.score` is 0-100. `readiness.band`: `high` (67-100), `medium` (34-66), `low` (0-33) unless the
  provider has its own bands, in which case the provider's band maps onto these three. `readiness.state`:
  `scored`, `pending` (not scored yet), `unscorable`.
- `sleep` is the main sleep that ended on `date`; `sleep.naps` lists shorter sleeps. Hours are decimal hours;
  `asleep_h` excludes awake time. `sleep.score` is the provider's sleep score or performance, 0-100.
  `sleep.need_h` is the provider's sleep need for that night or `null` when it has none (Fitbit); a front end then
  judges the night by hours asleep instead of the share of need.
- `load` is the day's effort on the provider's own scale; `load.max` and `load.label` repeat `scales.load`.
- `calories_kcal` is the day's total energy; `energy_kj` is the same in kilojoules when the provider reports kJ.
- `workouts[].zones_min` is minutes in heart-rate zones 0-5 (or `null`).
- `extras` holds provider-specific fields under the provider's own key (`extras.whoop`, `extras.oura`...). Front
  ends may show them but never depend on them.

## MCP (connector side)

Connectors expose the same narrow tools so the agent can check them without the front end:
`health_data_status` (the status above), `health_data_sync` (ask for fresh data now) and `health_data_days`
(the `/api/days` read with the same arguments). Owner-facing questions ("how did I sleep?") go to the front end's
tools, not the connector's.

## Versioning

Additive fields keep `wizard.health-data/1`. Renaming or removing a field, or changing a meaning, is
`wizard.health-data/2`; a front end that sees a contract it does not know marks the source "needs an update".
