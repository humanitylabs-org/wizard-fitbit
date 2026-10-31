---
name: wizard-fitbit
description: Use when the owner asks about his Fitbit or Pixel Watch connection, setup or sync. Health questions go to Health.
version: 0.1.1
author: Humanity Labs
license: GPL-3.0-only
metadata:
  hermes:
    tags: [fitbit, google-health, health-data, connector]
    related_skills: [wizard-health]
---
# Fitbit (connector)

The box's Fitbit / Pixel Watch connection (`health-data` spot, contract `wizard.health-data/1`). Data comes from the Google Health API (Google switched the old Fitbit Web API off in October 2026). It holds the Google sign-in and a synced copy; the **Health** app (`/wizard-health/`) shows the data and answers health questions. Tools: `mcp__wizard__wizard_fitbit__<tool>`.

- Questions about sleep, HRV, resting heart rate, activity, workouts or trends: use the Health app's tools, not these.
- This source has **no readiness/recovery score** (Google's API has no Fitbit Daily Readiness) and no Sleep Score or sleep need. Never estimate one. Activity is **Active Zone Minutes** (1 per fat-burn minute, 2 per cardio/peak minute; daily goal 22), not WHOOP strain.
- "Is my Fitbit connected / when did it last sync": `health_data_status`. `connected`, `syncing` or `rate_limited` = data is flowing; `not_configured` or `needs_login` = the owner must finish or redo setup. `setup.missing_scopes` lists data Google isn't sharing (he unticked a box); `setup.signin_ends` is when a Testing-mode Google project ends the sign-in (every 7 days).
- Setup, reconnecting or replacing keys: send the owner the app link `[Fitbit](/wizard-fitbit/)` and say its screen walks him through it (five steps: Fitbit on a Google account, Google Cloud project + enable Google Health API, Google sign-in setup with the copy buttons, OAuth client + paste Client ID and secret, Connect with Google). Never walk him through Google Cloud in chat, and never ask for or accept a Client ID, Client secret, password, sign-in code or the localhost callback address in chat; those go only into the app's own screen.
- `health_data_sync` asks for fresh data now (the app also syncs every 20 minutes); it returns at once.
- `health_data_days` is the raw contract read (normalized days, paged, `changed_since` cursor). Use it only to check what the connector holds.
- Scripts on the box can read the same contract at `http://127.0.0.1:8830/api/status` and `/api/days`.
