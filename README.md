# Fitbit (Wizard connector)

A Wizard OS connector app for one Fitbit or Pixel Watch account. It reads the owner's data from the **Google Health API** (`health.googleapis.com/v4`), the official replacement for the legacy Fitbit Web API, which Google stopped supporting on 30 Sep 2026 and switched off at the end of October 2026. It keeps a private synced copy and offers it read-only to other apps on the same box through the `health-data` spot, contract [`wizard.health-data/1`](docs/health-data-contract.md). The [Health](https://github.com/oscarhenrycollins/wizard-health) app is the screen for the data; this app is only the connection.

**Each owner needs their own developer credentials** (a free Google Cloud project and OAuth client). Google Health API scopes are "restricted": an unverified project works for its own test users (up to 100), which is exactly the personal case. While the project is in Testing mode Google ends the sign-in after 7 days (`refresh_token_expires_in`), and the app asks the owner to connect again.

## Setup (all on the app's own screen)

1. Fitbit / Pixel Watch syncing to a personal Google account (Fitbit accounts moved to Google in 2026). No Premium needed.
2. Google Cloud: create a project, enable the **Google Health API**.
3. Google Auth Platform: Get started (app name, support email, Audience **External**, contact email), add yourself under Audience > Test users, and under Data access add the three read-only scopes (copy button): `googlehealth.activity_and_fitness.readonly`, `googlehealth.health_metrics_and_measurements.readonly`, `googlehealth.sleep.readonly`.
4. Clients > Create client > **Web application**, Authorized redirect URI = the value shown (default `http://localhost:3000/callback`). Paste Client ID and Client secret (stored server-side, 0600, never echoed). The app tests them by exchanging a made-up code at Google's token endpoint (`invalid_grant` = keys valid, `invalid_client` = wrong).
5. Connect with Google: consent opens in the owner's own browser (authorization code + PKCE S256, `access_type=offline`, `prompt=consent`). Google warns the app is unverified (it's the owner's own project): Continue, tick all boxes. The browser lands on `http://localhost:3000/callback?...` (doesn't load); paste that address back. Over https the screen also offers the box's own address (`https://<box>/wizard-fitbit/callback`), where the app finishes by itself.

## What it syncs

Backfill of 365 days (`FITBIT_HISTORY_DAYS`), then every 20 minutes with a 4-day overlap; records Google deleted in the window are removed here too. Lists: `sleep`, `exercise`, `daily-heart-rate-variability`, `daily-resting-heart-rate`, `daily-oxygen-saturation`, `daily-respiratory-rate`, `daily-sleep-temperature-derivations` (civil-date filters, paged with `nextPageToken`). Daily rollups (`dailyRollUp`): `active-zone-minutes`, `steps` (90-day windows), `total-calories` (14-day windows, Google's limit). 401 refreshes once; 429/5xx back off (Retry-After), then pause 15 minutes; a 403 for one data type (scope not granted) skips that type and names it on the screen.

## Contract mapping

| contract | Google Health API |
|---|---|
| `readiness` | `null` always (no Daily Readiness in the API); `scales.readiness.available: false` |
| `hrv_ms` | `dailyHeartRateVariability.averageHeartRateVariabilityMilliseconds` (RMSSD) |
| `resting_hr_bpm` | `dailyRestingHeartRate.beatsPerMinute` |
| `sleep` | main sleep (`metadata.mainSleep`, else the longest) ending on the date: `summary.minutesAsleep` / `minutesInSleepPeriod` / `minutesAwake`, `stagesSummary` LIGHT/DEEP/REM, efficiency = asleep / in bed; `score` and `need_h` are `null`; other sleeps are `naps` |
| `load` | Active Zone Minutes = fat burn + cardio + peak from the `active-zone-minutes` daily rollup; `max: null`, `scale_max: 60`, unit `min` |
| `steps`, `calories_kcal` | `steps.countSum`, `totalCalories.kcalSum` |
| `spo2_pct`, `respiratory_rate`, `skin_temp_c` / `skin_temp_delta_c` | daily oxygen saturation average, daily breaths per minute, nightly temperature (minus baseline) |
| `workouts` | `exercise` sessions: display name, active duration, AZM, average HR, calories, distance; Fitbit's four HR zones in `extras.fitbit.zones_min` |

Activity targets per recovery band (`scales.load.targets`, see the contract doc): Ready 22-45 AZM, So-so 10-22, Run down under 10, "too much" more than 15 above; anchored on Fitbit's default daily goal of 22 AZM (~150 a week). With no readiness from this source, Health shows activity as "no recovery data".

## Interfaces

- Screen: `/wizard-fitbit/` (status, Sync now, Connect again, Disconnect, guided setup).
- Contract (box-only): `GET /api/status`, `GET /api/days?from&to&limit&page&changed_since`.
- Setup API used by the screen: `GET /api/setup`, `POST /api/keys`, `/api/keys/test`, `/api/keys/forget`, `/api/oauth/start`, `/api/oauth/finish`, `/api/sync`, `/api/disconnect`; `GET /callback`.
- MCP (`POST /mcp`, box-only): `health_data_status`, `health_data_sync`, `health_data_days`.

## Development

`python3 -m unittest discover -s tests` runs the tests against a fake Google built from the published docs and samples. `FITBIT_LIVE_TEST=1` adds one live call: the Test button against Google's real token endpoint with made-up keys (expects "not accepted"). `python3 tools/package.py` refreshes the inline Runtipi payload; `--check` verifies it.

Not verified against a real account: the author had no Fitbit. Field names and shapes come from Google's reference documentation (Oct 2026).
