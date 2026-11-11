"""Fitbit: the Fitbit / Pixel Watch connector for a Wizard box (contract wizard.health-data/1).

Google turned the legacy Fitbit Web API off in autumn 2026 (support ended 30 Sep 2026, endpoints switched off at the
end of October). Fitbit and Pixel Watch data is now read through the Google Health API (health.googleapis.com/v4)
with Google OAuth 2.0. That is what this app uses.

Setup happens on this app's own screen. The owner creates his own Google Cloud project, enables the Google Health
API, sets up Google sign-in (External audience, himself as a test user, three read-only scopes) and creates a
"Web application" OAuth client. He pastes its Client ID and Client secret here (stored server-side, never sent
back), then signs in with Google once (authorization code + PKCE, access_type=offline). Google sends the one-time
code to the Redirect URL he registered: either this app's own https address (the app finishes on its own) or
http://localhost:3000/callback, in which case he pastes the address the browser landed on back into the app.

The app keeps a local SQLite copy of sleep sessions, exercises, daily HRV / resting heart rate / SpO2 / breathing
rate / sleep temperature, and daily rollups of Active Zone Minutes, steps and calories: a history backfill after
sign-in, then a sync every 20 minutes with a few days of overlap so late or edited records are updated (and records
deleted on Google's side disappear here too). Other apps on the box (the Health app) read the copy through the
box-only health-data contract: GET /api/status and GET /api/days (docs/health-data-contract.md).

What Google's API does NOT offer: Fitbit's Daily Readiness score, the Sleep Score and a nightly sleep need. This
app never invents them: `readiness` is null and `scales.readiness.available` is false. Activity is Active Zone
Minutes on its own scale (see AZM_* below and the contract doc), never relabelled as strain.

Stdlib only. Secrets live in DATA_DIR (0600 files, 0700 folder) and are never returned by any API or tool.
"""
import base64
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

ROOT = Path(__file__).resolve().parent.parent if '__file__' in globals() else Path(os.environ.get('APP_ROOT', '.')).resolve()
DATA = Path(os.environ.get('DATA_DIR', ROOT / '.data'))
APP_ID = 'wizard-fitbit'
VERSION = '0.1.2'
PREFIX = '/' + APP_ID
PORT = os.environ.get('PORT', '8830')
AUTH_URL = os.environ.get('FITBIT_AUTH_URL', 'https://accounts.google.com/o/oauth2/v2/auth')
TOKEN_URL = os.environ.get('FITBIT_TOKEN_URL', 'https://oauth2.googleapis.com/token')
REVOKE_URL = os.environ.get('FITBIT_REVOKE_URL', 'https://oauth2.googleapis.com/revoke')
API_BASE = os.environ.get('FITBIT_API_BASE', 'https://health.googleapis.com/v4').rstrip('/')
CONSOLE = 'https://console.cloud.google.com'
LINKS = {
    'project': CONSOLE + '/projectcreate',
    'enable_api': CONSOLE + '/apis/library/health.googleapis.com',
    'auth_platform': CONSOLE + '/auth/overview',
    'audience': CONSOLE + '/auth/audience',
    'data_access': CONSOLE + '/auth/scopes',
    'clients': CONSOLE + '/auth/clients',
    'move_account': 'https://support.google.com/fitbit/answer/14237024',
}
# Where a person without a device goes. One constant so a referral link can be dropped in later.
SIGNUP_URL = os.environ.get('FITBIT_SIGNUP_URL', 'https://store.google.com/category/watches_trackers')
DEFAULT_REDIRECT = 'http://localhost:3000/callback'
SUGGESTED_APP_NAME = 'Wizard Health'     # project, consent screen and client name; any name works
SCOPE_ROOT = 'https://www.googleapis.com/auth/googlehealth.'
SCOPE_NAMES = {'activity_and_fitness.readonly': 'Activity', 'health_metrics_and_measurements.readonly': 'Heart and body',
               'sleep.readonly': 'Sleep'}
READ_SCOPES = [SCOPE_ROOT + s for s in SCOPE_NAMES]
SCOPE = ' '.join(READ_SCOPES)
SYNC_MINUTES = float(os.environ.get('FITBIT_SYNC_MINUTES', '20'))
STATE_TTL = float(os.environ.get('FITBIT_STATE_TTL', str(2 * 3600)))   # a sign-in started here stays valid for two hours
OVERLAP_DAYS = int(os.environ.get('FITBIT_OVERLAP_DAYS', '4'))
HISTORY_DAYS = int(os.environ.get('FITBIT_HISTORY_DAYS', '365'))
REQUEST_GAP = float(os.environ.get('FITBIT_REQUEST_GAP', '0.25'))   # ~240 a minute, under Google's 300 per user
RETRIES = int(os.environ.get('FITBIT_RETRIES', '5'))
BACKOFF = float(os.environ.get('FITBIT_BACKOFF', '5'))
UA = f'{APP_ID}/{VERSION} (+Wizard OS)'
STATIC = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml'}
FORWARDED = ('tailscale-user-login', 'x-forwarded-for', 'forwarded', 'x-real-ip', 'cf-connecting-ip')
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

# Collections read from the Google Health API: kind, dataType, filter member, page size, scope key.
# Filters follow https://developers.google.com/health/filters (civil dates; sleep is filtered on its end).
LISTS = [
    ('sleep', 'sleep', 'sleep.interval.civil_end_time', 25, 'sleep.readonly'),
    ('exercise', 'exercise', 'exercise.interval.civil_start_time', 25, 'activity_and_fitness.readonly'),
    ('hrv', 'daily-heart-rate-variability', 'daily_heart_rate_variability.date', 1000, 'health_metrics_and_measurements.readonly'),
    ('rhr', 'daily-resting-heart-rate', 'daily_resting_heart_rate.date', 1000, 'health_metrics_and_measurements.readonly'),
    ('spo2', 'daily-oxygen-saturation', 'daily_oxygen_saturation.date', 1000, 'health_metrics_and_measurements.readonly'),
    ('resp', 'daily-respiratory-rate', 'daily_respiratory_rate.date', 1000, 'health_metrics_and_measurements.readonly'),
    ('temp', 'daily-sleep-temperature-derivations', 'daily_sleep_temperature_derivations.date', 1000, 'health_metrics_and_measurements.readonly'),
]
# Daily rollups: kind, dataType, max days per request (Google: 14 for total-calories, 90 for the rest), scope key
ROLLUPS = [
    ('azm', 'active-zone-minutes', 90, 'activity_and_fitness.readonly'),
    ('steps', 'steps', 90, 'activity_and_fitness.readonly'),
    ('calories', 'total-calories', 14, 'activity_and_fitness.readonly'),
]
SCOPE_OF = {x[0]: x[-1] for x in LISTS + ROLLUPS}
CHUNK_DAYS = 90
LOCK = threading.RLock()          # local files + database
TOKEN_LOCK = threading.Lock()     # one refresh at a time
SYNC_LOCK = threading.Lock()
WAKE = threading.Event()


class Problem(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class GoogleError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ---------------- private storage ----------------

def _private():
    DATA.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA, 0o700)
    return DATA


def _write_secret(name, text):
    p = _private() / name
    tmp = p.with_name('.' + name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def _read(name):
    try:
        return json.loads((DATA / name).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _shred(name):
    p = DATA / name
    try:
        size = p.stat().st_size
        with open(p, 'r+b') as f:
            f.write(b'\0' * size)
            f.flush()
            os.fsync(f.fileno())
        p.unlink()
    except FileNotFoundError:
        pass


def _keys():
    return _read('client.json')


def _tokens():
    return _read('tokens.json')


def _save_tokens(t):
    with LOCK:
        if t:
            _write_secret('tokens.json', json.dumps(t))
        else:
            _shred('tokens.json')


def _state():
    return _read('state.json')


def _update_state(**kv):
    with LOCK:
        s = _state()
        for k, v in kv.items():
            if v is None:
                s.pop(k, None)
            else:
                s[k] = v
        _write_secret('state.json', json.dumps(s))
        return s


def secure_files():
    """Runtipi loosens app-data permissions; tighten them again on every start."""
    db().close()
    for p in DATA.iterdir():
        if p.is_file():
            os.chmod(p, 0o600)


def db():
    path = _private() / 'fitbit.sqlite3'
    fresh = not path.exists()
    c = sqlite3.connect(path, timeout=15)
    if fresh:
        os.chmod(path, 0o600)
    c.row_factory = sqlite3.Row
    c.executescript('''
      CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL, id TEXT NOT NULL, day TEXT NOT NULL, start TEXT,
        data TEXT NOT NULL, changed_seq INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (kind, id));
      CREATE INDEX IF NOT EXISTS records_day ON records(kind, day);
      CREATE TABLE IF NOT EXISTS day_marks (day TEXT PRIMARY KEY, changed_seq INTEGER NOT NULL, changed_at TEXT);
      CREATE INDEX IF NOT EXISTS day_marks_seq ON day_marks(changed_seq);
      CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, data TEXT NOT NULL);
    ''')
    return c


def _next_seq(c):
    n = int(json.loads((c.execute("SELECT data FROM meta WHERE key='seq'").fetchone() or ['0'])[0])) + 1
    c.execute("INSERT OR REPLACE INTO meta (key,data) VALUES ('seq',?)", (str(n),))
    return n


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(t):
    return t.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def _mark(c, day, seq):
    c.execute('''INSERT INTO day_marks (day, changed_seq, changed_at) VALUES (?,?,?)
                 ON CONFLICT(day) DO UPDATE SET changed_seq=excluded.changed_seq, changed_at=excluded.changed_at''',
              (day, seq, _iso(_now())))


def _parse(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(re.sub(r'(\.\d{6})\d+', r'\1', str(s)).replace('Z', '+00:00'))
    except ValueError:
        return None


def _ago(iso):
    t = _parse(iso)
    return (_now() - t).total_seconds() if t else 1e12


# ---------------- Google HTTP ----------------

def _http(method, url, form=None, body=None, headers=None, timeout=45):
    h = {'User-Agent': UA, 'Accept': 'application/json'}
    data = None
    if form is not None:
        data = urlencode(form).encode()
        h['Content-Type'] = 'application/x-www-form-urlencoded'
    elif body is not None:
        data = json.dumps(body).encode()
        h['Content-Type'] = 'application/json'
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})
    except (urllib.error.URLError, TimeoutError, OSError):
        raise GoogleError(0, 'unreachable', 'Google did not answer. Check the box is online; the app will retry.') from None


def _json(b):
    try:
        d = json.loads(b or b'{}')
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _token_call(form):
    k = _keys()
    if not k.get('client_id') or not k.get('client_secret'):
        raise Problem(409, 'Save your Google Client ID and Client secret first (step 4).')
    form = dict(form, client_id=k['client_id'], client_secret=k['client_secret'])
    status, body, _ = _http('POST', TOKEN_URL, form=form)
    return status, _json(body)


def test_keys():
    """Exchange a made-up code: Google answers invalid_grant when the client and secret are right, invalid_client
    (401, "The OAuth client was not found." / "Unauthorized") when they are not."""
    k = _keys()
    if not k.get('client_id') or not k.get('client_secret'):
        raise Problem(409, 'Save your Google Client ID and Client secret first.')
    status, d = _token_call({'grant_type': 'authorization_code', 'code': 'wizard-key-check-' + secrets.token_hex(8),
                             'redirect_uri': k.get('redirect_uri') or DEFAULT_REDIRECT,
                             'code_verifier': secrets.token_urlsafe(48)})
    err = str(d.get('error') or '')
    if err == 'invalid_grant':
        result, msg = 'valid', 'Google recognised your Client ID and Client secret. You can connect now.'
    elif err in ('invalid_client', 'unauthorized_client'):
        result, msg = 'invalid', 'Google did not accept this Client ID and Client secret. Copy both again from your OAuth client and save.'
    elif err == 'redirect_uri_mismatch':
        result, msg = 'invalid', 'Google does not know this Redirect URL. Add it exactly under Authorized redirect URIs on your OAuth client.'
    elif status == 429:
        result, msg = 'unknown', 'Google is busy right now. Try the test again in a minute.'
    else:
        result, msg = 'unknown', 'Google gave an unexpected answer, so the keys could not be confirmed. Try again shortly.'
    with LOCK:
        k = _keys()
        if k:
            k.update(verified=result, checked_at=_iso(_now()))
            _write_secret('client.json', json.dumps(k))
    return {'result': result, 'message': msg}


def save_keys(body):
    cid = str(body.get('client_id') or '').strip()
    secret = str(body.get('client_secret') or '').strip()
    redirect = str(body.get('redirect_uri') or '').strip() or DEFAULT_REDIRECT
    if not re.fullmatch(r'[A-Za-z0-9._-]{8,200}\.apps\.googleusercontent\.com', cid):
        raise Problem(400, 'That Client ID does not look right. It ends in .apps.googleusercontent.com; copy it again from your OAuth client.')
    if not re.fullmatch(r'[\x21-\x7e]{8,300}', secret):
        raise Problem(400, 'That Client secret does not look right. Copy it again from your OAuth client.')
    u = urlsplit(redirect)
    if u.scheme not in ('http', 'https') or not u.netloc or len(redirect) > 300:
        raise Problem(400, 'The Redirect URL must be the exact http(s) address you added to your OAuth client.')
    if u.scheme == 'http' and u.hostname not in ('localhost', '127.0.0.1'):
        raise Problem(400, 'Google accepts plain http only for localhost. Use https or http://localhost:3000/callback.')
    with LOCK:
        old = _keys()
        changed = old.get('client_id') != cid or old.get('client_secret') != secret
        _write_secret('client.json', json.dumps({'client_id': cid, 'client_secret': secret, 'redirect_uri': redirect,
                                                 'saved_at': _iso(_now()), 'verified': None}))
        if changed and _tokens():
            _save_tokens(None)   # tokens belong to the old OAuth client; sign in again with the new one
            _update_state(auth='needs_login', pending=None)
    return setup_info()


def forget_keys():
    with LOCK:
        _shred('client.json')
        _save_tokens(None)
        _update_state(pending=None, auth=None)
    return setup_info()


def _mask(v):
    v = str(v or '').replace('.apps.googleusercontent.com', '')
    return ('•' * 6 + v[-4:]) if len(v) > 8 else '•' * 6


def setup_info():
    k = _keys()
    t = _tokens()
    return {'links': LINKS, 'signup_url': SIGNUP_URL, 'suggested_name': SUGGESTED_APP_NAME,
            'default_redirect': DEFAULT_REDIRECT, 'scopes': READ_SCOPES, 'scope_names': list(SCOPE_NAMES.values()),
            'keys_saved': bool(k.get('client_id') and k.get('client_secret')),
            'client_id_hint': _mask(k.get('client_id')) if k.get('client_id') else None, 'secret_saved': bool(k.get('client_secret')),
            'redirect_uri': k.get('redirect_uri'), 'verified': k.get('verified'), 'checked_at': k.get('checked_at'),
            'connected': bool(t.get('refresh_token') or t.get('access_token'))}


# ---------------- OAuth sign-in (authorization code + PKCE) ----------------

def _challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')


def login_start():
    k = _keys()
    if not k.get('client_id') or not k.get('client_secret'):
        raise Problem(409, 'Save your Google Client ID and Client secret first (step 4).')
    state, verifier = secrets.token_hex(16), secrets.token_urlsafe(48)
    with LOCK:
        pend = [p for p in (_state().get('pending') or []) if _ago(p.get('at')) < STATE_TTL][-4:]
        pend.append({'state': state, 'verifier': verifier, 'at': _iso(_now())})
        _update_state(pending=pend)
    q = {'response_type': 'code', 'client_id': k['client_id'], 'redirect_uri': k.get('redirect_uri') or DEFAULT_REDIRECT,
         'scope': SCOPE, 'state': state, 'access_type': 'offline', 'prompt': 'consent',
         'code_challenge': _challenge(verifier), 'code_challenge_method': 'S256'}
    return {'authorize_url': AUTH_URL + '?' + urlencode(q, quote_via=quote), 'redirect_uri': q['redirect_uri'],
            'paste_back': urlsplit(q['redirect_uri']).hostname in ('localhost', '127.0.0.1')}


def login_finish(pasted):
    raw = str(pasted or '').strip()
    if not raw or len(raw) > 4096:
        raise Problem(400, 'Paste the whole address from the browser bar after you tapped Continue.')
    u = urlsplit(raw if '://' in raw else 'http://x/?' + raw.lstrip('?'))
    q = {a: b[0] for a, b in parse_qs(u.query).items()}
    if q.get('error'):
        _update_state(pending=None)
        msg = 'You chose not to allow access.' if q['error'] == 'access_denied' else 'Google did not finish the sign-in.'
        raise Problem(400, msg + ' Tap Connect with Google to try again.')
    code, state = q.get('code'), q.get('state')
    if not code or not state:
        raise Problem(400, 'That address has no sign-in code. Paste the whole address, starting with http.')
    with LOCK:
        pend = [p for p in (_state().get('pending') or []) if _ago(p.get('at')) < STATE_TTL]
        hit = next((p for p in pend if secrets.compare_digest(p['state'], state)), None)
        if not hit:
            raise Problem(400, 'This sign-in link expired or was already used. Tap Connect with Google to start again.')
        _update_state(pending=[p for p in pend if p['state'] != state])
    k = _keys()
    status, d = _token_call({'grant_type': 'authorization_code', 'code': code, 'code_verifier': hit['verifier'],
                             'redirect_uri': k.get('redirect_uri') or DEFAULT_REDIRECT})
    if status != 200 or not d.get('access_token'):
        err = d.get('error')
        if err == 'invalid_client':
            raise Problem(400, 'Google did not accept the saved Client ID and Client secret. Replace the keys and try again.')
        if err in ('invalid_grant', 'redirect_uri_mismatch'):
            raise Problem(400, 'Google refused this code. It may have been used already or the Redirect URL does not match. Tap Connect with Google again.')
        raise Problem(502, 'Google did not finish the sign-in. Try again in a minute.')
    _store_tokens(d)
    # Without a refresh token the sign-in would stop after an hour (prompt=consent should always give one).
    note = None if d.get('refresh_token') else 'Google gave no long-lived sign-in, so syncing stops within an hour. Tap Connect with Google again.'
    _update_state(auth='connected', last_error=note, connected_at=_iso(_now()), missing_scopes=None, denied=None)
    start_sync_thread(full=not _state().get('backfilled'))
    return status_info()


def _store_tokens(d, old=None):
    old = old or {}
    t = {'access_token': d['access_token'], 'refresh_token': d.get('refresh_token') or old.get('refresh_token'),
         'expires_at': time.time() + int(d.get('expires_in') or 3600), 'scope': d.get('scope') or old.get('scope') or SCOPE}
    if d.get('refresh_token_expires_in'):          # Google "Testing" projects: the sign-in ends after 7 days
        t['refresh_expires_at'] = time.time() + int(d['refresh_token_expires_in'])
    elif old.get('refresh_expires_at') and not d.get('refresh_token'):
        t['refresh_expires_at'] = old['refresh_expires_at']
    _save_tokens(t)
    return t


def _granted():
    """Scope keys (e.g. 'sleep.readonly') this sign-in actually holds; Google lets the owner untick boxes."""
    sc = str(_tokens().get('scope') or '').split()
    return {s[len(SCOPE_ROOT):] for s in sc if s.startswith(SCOPE_ROOT)}


def _access_token(force=False):
    with TOKEN_LOCK:
        t = _tokens()
        if not t.get('access_token') and not t.get('refresh_token'):
            raise Problem(409, 'Not connected to Google. Open the Fitbit app and tap Connect with Google.')
        if not force and t.get('access_token') and t.get('expires_at', 0) - 120 > time.time():
            return t['access_token']
        if not t.get('refresh_token'):
            _needs_login('The Google sign-in expired. Connect again in the Fitbit app.')
        status, d = _token_call({'grant_type': 'refresh_token', 'refresh_token': t['refresh_token']})
        if status == 200 and d.get('access_token'):
            return _store_tokens(d, t)['access_token']   # persist a rotated refresh token (if Google sends one) first
        if status in (400, 401) and d.get('error') in ('invalid_grant', 'invalid_request', 'invalid_token', 'unauthorized_client', 'invalid_client'):
            _needs_login('Google ended this sign-in (in Testing mode it does so after 7 days). Connect again in the Fitbit app.')
        raise GoogleError(status, 'refresh_failed', 'Google could not renew the sign-in right now. The app will retry.')


def _needs_login(msg):
    _save_tokens(None)
    _update_state(auth='needs_login', last_error=msg)
    raise Problem(401, msg)


def _reason(body):
    e = _json(body).get('error') or {}
    if not isinstance(e, dict):
        return ''
    for det in e.get('details') or []:
        if isinstance(det, dict) and det.get('reason'):
            return str(det['reason'])
    return str(e.get('status') or '')


def api_call(method, path, params=None, body=None):
    url = API_BASE + path + ('?' + urlencode(params, quote_via=quote) if params else '')
    refreshed, status = False, 0
    for attempt in range(RETRIES + 1):
        status, raw, headers = _http(method, url, body=body, headers={'Authorization': 'Bearer ' + _access_token()})
        if status == 200:
            return _json(raw)
        if status == 401 and not refreshed:
            refreshed = True
            _access_token(force=True)
            continue
        if status == 404:
            return None
        if status == 429 or status >= 500:
            if attempt >= RETRIES:
                break
            try:
                wait = float({k.lower(): v for k, v in headers.items()}.get('retry-after') or 0)
            except ValueError:
                wait = 0
            time.sleep(min(120.0, max(wait, BACKOFF * (2 ** attempt))))
            continue
        reason = _reason(raw)
        if status == 403:
            raise GoogleError(403, 'denied', reason or 'PERMISSION_DENIED')
        if status == 400 and reason == 'ACCOUNT_NOT_LINKED':
            raise GoogleError(400, 'not_linked', 'This Google account has no Fitbit data. Connect with the Google account your Fitbit or Pixel Watch uses.')
        raise GoogleError(status, 'api_error', f'Google answered {status} for {path}' + (f' ({reason}).' if reason else '.'))
    if status == 429:
        _update_state(rate_limited_until=time.time() + 900)
        raise GoogleError(429, 'rate_limited', 'Google asked the app to slow down. Syncing resumes in about 15 minutes.')
    raise GoogleError(status, 'api_error', f'Google answered {status} for {path}. The app will retry.')


# ---------------- record parsing ----------------

def _date(d):
    """Google `Date` object {year, month, day} -> 'YYYY-MM-DD'."""
    if not isinstance(d, dict) or not d.get('year') or not d.get('month') or not d.get('day'):
        return None
    return f"{int(d['year']):04d}-{int(d['month']):02d}-{int(d['day']):02d}"


def _ymd(day):
    y, m, d = (int(x) for x in day.split('-'))
    return {'year': y, 'month': m, 'day': d}


def _secs(s):
    """Google Duration '-14400s' / '3.5s' -> float seconds."""
    m = re.fullmatch(r'(-?\d+(?:\.\d+)?)s', str(s or ''))
    return float(m.group(1)) if m else None


def _civil(interval, end=False):
    """The owner's local calendar date at the start (or end) of an interval: civil time if Google sent it, else
    the physical time shifted by its UTC offset."""
    if not isinstance(interval, dict):
        return None
    civil = interval.get('civilEndTime' if end else 'civilStartTime') or {}
    day = _date(civil.get('date'))
    if day:
        return day
    t = _parse(interval.get('endTime' if end else 'startTime'))
    off = _secs(interval.get('endUtcOffset' if end else 'startUtcOffset')) or 0
    return (t + dt.timedelta(seconds=off)).date().isoformat() if t else None


def _int(v):
    try:
        return int(str(v)) if v is not None and str(v) != '' else None
    except ValueError:
        return None


def _num(v, n=1):
    if isinstance(v, str):
        try:
            v = float(v)
        except ValueError:
            return None
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    return round(v) if n == 0 else round(v, n)


DAILY_FIELD = {'hrv': 'dailyHeartRateVariability', 'rhr': 'dailyRestingHeartRate', 'spo2': 'dailyOxygenSaturation',
               'resp': 'dailyRespiratoryRate', 'temp': 'dailySleepTemperatureDerivations'}
ROLLUP_FIELD = {'azm': 'activeZoneMinutes', 'steps': 'steps', 'calories': 'totalCalories'}


def _row(kind, dp):
    """(id, day, start) for one Google data point, or None if it can't be placed on a day."""
    if kind in ('sleep', 'exercise'):
        iv = (dp.get(kind) or {}).get('interval') or {}
        day = _civil(iv, end=(kind == 'sleep'))    # a night belongs to the day you woke up
        rid = dp.get('name') or (kind + ':' + str(iv.get('startTime')))
        return (rid, day, iv.get('startTime')) if day else None
    day = _date((dp.get(DAILY_FIELD[kind]) or {}).get('date'))
    return (kind + ':' + day, day, None) if day else None


def _rollup_row(kind, rp):
    if not isinstance(rp.get(ROLLUP_FIELD[kind]), dict):   # Google leaves the value unset when the device was not worn
        return None
    day = _date((rp.get('civilStartTime') or {}).get('date'))
    return (kind + ':' + day, day, None) if day else None


def _save(kind, items, lo, hi):
    """Upsert one collection for the window [lo, hi] and drop what Google no longer has in that window.
    A day's mark moves only when its data really changed, so an overlapping re-sync re-emits nothing."""
    with LOCK:
        c = db()
        with c:
            seq = _next_seq(c)
            seen = set()
            for (rid, day, start), data in items:
                text = json.dumps(data, sort_keys=True)
                seen.add(rid)
                old = c.execute('SELECT day, data FROM records WHERE kind=? AND id=?', (kind, rid)).fetchone()
                if old and old['data'] == text and old['day'] == day:
                    continue
                c.execute('''INSERT INTO records (kind,id,day,start,data,changed_seq) VALUES (?,?,?,?,?,?)
                             ON CONFLICT(kind,id) DO UPDATE SET day=excluded.day, start=excluded.start, data=excluded.data,
                               changed_seq=excluded.changed_seq''', (kind, rid, day, start, text, seq))
                _mark(c, day, seq)
                if old and old['day'] != day:
                    _mark(c, old['day'], seq)
            for r in c.execute('SELECT id, day FROM records WHERE kind=? AND day>=? AND day<=?', (kind, lo, hi)).fetchall():
                if r['id'] not in seen:
                    c.execute('DELETE FROM records WHERE kind=? AND id=?', (kind, r['id']))
                    _mark(c, r['day'], seq)
        c.close()


def _windows(lo, hi, size):
    d0, d1 = dt.date.fromisoformat(lo), dt.date.fromisoformat(hi)
    while d0 <= d1:
        e = min(d1, d0 + dt.timedelta(days=size - 1))
        yield d0.isoformat(), e.isoformat()
        d0 = e + dt.timedelta(days=1)


def _next_day(day):
    return (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()


def _pull_list(kind, dtype, member, size, lo, hi):
    items, token = [], None
    flt = f'{member} >= "{lo}" AND {member} < "{_next_day(hi)}"'
    for _ in range(2000):
        p = {'filter': flt, 'pageSize': size}
        if token:
            p['pageToken'] = token
        d = api_call('GET', f'/users/me/dataTypes/{dtype}/dataPoints', p) or {}
        for dp in d.get('dataPoints') or []:
            if isinstance(dp, dict):
                r = _row(kind, dp)
                if r:
                    items.append((r, dp))
        token = d.get('nextPageToken')
        if not token:
            break
        time.sleep(REQUEST_GAP)
    return items


def _pull_rollup(kind, dtype, lo, hi):
    days = (dt.date.fromisoformat(hi) - dt.date.fromisoformat(lo)).days + 1
    body = {'range': {'start': {'date': _ymd(lo), 'time': {}}, 'end': {'date': _ymd(_next_day(hi)), 'time': {}}},
            'windowSizeDays': 1, 'pageSize': days}
    items, token = [], None
    for _ in range(100):
        b = dict(body, **({'pageToken': token} if token else {}))
        d = api_call('POST', f'/users/me/dataTypes/{dtype}/dataPoints:dailyRollUp', body=b) or {}
        for rp in d.get('rollupDataPoints') or []:
            if isinstance(rp, dict):
                r = _rollup_row(kind, rp)
                if r and lo <= r[1] <= hi:
                    items.append((r, rp))
        token = d.get('nextPageToken')
        if not token:
            break
        time.sleep(REQUEST_GAP)
    return items


def sync(full=False):
    if not SYNC_LOCK.acquire(blocking=False):
        return dict(status_info(), note='A sync is already running.')
    try:
        st = _state()
        if (st.get('rate_limited_until') or 0) > time.time():
            raise GoogleError(429, 'rate_limited', 'Google asked the app to slow down. Syncing resumes shortly.')
        full = full or not st.get('backfilled')
        _update_state(syncing=_iso(_now()), sync_kind='backfill' if full else 'incremental')
        today = (_now() + dt.timedelta(hours=14)).date()          # the latest civil date anywhere on Earth
        if full:
            lo = (today - dt.timedelta(days=HISTORY_DAYS)).isoformat()
        else:
            last = _parse(st.get('last_sync')) or _now()
            lo = (last - dt.timedelta(days=OVERLAP_DAYS + 1)).date().isoformat()
        hi = today.isoformat()
        granted = _granted()
        counts, denied = {}, set()
        jobs = [(k, (lambda a, b, k=k, t=t, m=m, s=s: _pull_list(k, t, m, s, a, b)), CHUNK_DAYS) for k, t, m, s, _ in LISTS]
        jobs += [(k, (lambda a, b, k=k, t=t: _pull_rollup(k, t, a, b)), size) for k, t, size, _ in ROLLUPS]
        for kind, pull, size in jobs:
            if granted and SCOPE_OF[kind] not in granted:
                denied.add(kind)
                continue
            counts[kind] = 0
            for a, b in _windows(lo, hi, size):
                try:
                    items = pull(a, b)
                except GoogleError as e:
                    if e.status == 403:       # this data type is not shared with the app; keep going without it
                        denied.add(kind)
                        break
                    raise
                _save(kind, items, a, b)
                counts[kind] += len(items)
                _update_state(progress={'kind': kind, 'records': counts[kind]})
                time.sleep(REQUEST_GAP)
        missing = sorted({SCOPE_NAMES[SCOPE_OF[k]] for k in denied})
        _update_state(last_sync=_iso(_now()), last_error=None, backfilled=True, auth='connected', syncing=None, progress=None,
                      sync_kind=None, last_counts=counts, rate_limited_until=None, denied=sorted(denied) or None,
                      missing_scopes=missing or None)
        return dict(status_info(), pulled=counts)
    except (Problem, GoogleError) as e:
        _update_state(last_error=e.message, syncing=None, progress=None, sync_kind=None)
        raise
    finally:
        SYNC_LOCK.release()


def _safe_sync(full=False):
    try:
        sync(full)
    except (Problem, GoogleError):
        pass
    except Exception:   # never let a bad record stop the scheduler
        _update_state(last_error='Sync failed unexpectedly; it will retry.', syncing=None, progress=None)


def start_sync_thread(full=False):
    threading.Thread(target=_safe_sync, args=(full,), daemon=True).start()


def _scheduler():
    time.sleep(5)
    while True:
        st = _state()
        if _tokens().get('refresh_token') or _tokens().get('access_token'):
            due = not st.get('backfilled') or _ago(st.get('last_sync')) >= SYNC_MINUTES * 60
            if due and (st.get('rate_limited_until') or 0) <= time.time():
                _safe_sync(full=not st.get('backfilled'))
        WAKE.wait(60)
        WAKE.clear()


# ---------------- the health-data contract (wizard.health-data/1, see docs/health-data-contract.md) ----------------

CONTRACT = 'wizard.health-data/1'
SOURCE = {'id': APP_ID, 'provider': 'fitbit', 'name': 'Fitbit', 'app_path': PREFIX + '/'}
# Activity load = Active Zone Minutes (AZM): 1 per minute in the fat-burn zone, 2 per minute in cardio or peak.
# It has no ceiling, so `max` is null; `scale_max` is the top of the drawing scale (a day at or above it is "full").
# Targets per recovery band, in AZM, anchored on Fitbit's default daily goal of 22 (about 150 a week, the WHO/AHA
# guidance Fitbit's goal is built on). WHOOP's 0-21 strain targets are Ready 14-18, So-so 10-14, Run down < 10 with
# "too much" at more than 3 above; the AZM targets keep that shape: Ready = meet the goal up to about double,
# So-so = a light day up to the goal, Run down = under 10, and "too much" at more than 15 AZM above the band's top.
AZM_GOAL = 22
AZM_SCALE_MAX = 60
AZM_TARGETS = {'high': [22, 45], 'medium': [10, 22], 'low': [0, 10]}
AZM_OVER = 15
LOAD_LABEL = 'Active Zone Minutes'
SCALES = {'readiness': {'label': 'Readiness', 'min': 0, 'max': 100, 'available': False,
                        'note': "Google's API does not provide Fitbit's Daily Readiness score; this source sends no readiness."},
          'sleep_score': {'label': 'Sleep score', 'min': 0, 'max': 100, 'available': False},
          'load': {'label': LOAD_LABEL, 'short_label': 'AZM', 'unit': 'min', 'min': 0, 'max': None, 'scale_max': AZM_SCALE_MAX,
                   'daily_goal': AZM_GOAL, 'targets': AZM_TARGETS, 'over_margin': AZM_OVER}}


def _load(v):
    return {'value': v, 'max': None, 'label': LOAD_LABEL, 'scale_max': AZM_SCALE_MAX, 'unit': 'min'} if v is not None else None


def _dataset():
    st = _state()
    if not st.get('dataset'):
        st = _update_state(dataset=secrets.token_hex(12))
    return st['dataset']


def _h(minutes):
    return round(minutes / 60, 2) if isinstance(minutes, (int, float)) else None


def _sleep_len(s):
    v = _int((s.get('summary') or {}).get('minutesInSleepPeriod'))
    if v is not None:
        return v
    iv = s.get('interval') or {}
    a, b = _parse(iv.get('startTime')), _parse(iv.get('endTime'))
    return (b - a).total_seconds() / 60 if a and b else 0


def _n_sleep(s, main=True):
    sm = s.get('summary') or {}
    iv = s.get('interval') or {}
    md = s.get('metadata') or {}
    stage = {}
    for x in sm.get('stagesSummary') or []:
        if isinstance(x, dict) and x.get('type'):
            stage[x['type']] = _int(x.get('minutes'))
    if not stage:                                   # partial records: add up the stage segments
        for x in s.get('stages') or []:
            a, b = _parse(x.get('startTime')), _parse(x.get('endTime'))
            if a and b and x.get('type'):
                stage[x['type']] = (stage.get(x['type']) or 0) + (b - a).total_seconds() / 60
    asleep, in_bed = _int(sm.get('minutesAsleep')), _int(sm.get('minutesInSleepPeriod'))
    if asleep is None:
        parts = [stage.get(k) for k in ('LIGHT', 'DEEP', 'REM', 'ASLEEP') if stage.get(k) is not None]
        asleep = sum(parts) if parts else None
    if in_bed is None:
        in_bed = round(_sleep_len(s)) or None
    awake = _int(sm.get('minutesAwake'))
    if awake is None and stage.get('AWAKE') is not None:
        awake = stage['AWAKE']
    a, b = _parse(iv.get('startTime')), _parse(iv.get('endTime'))
    out = {'start': _iso(a) if a else None, 'end': _iso(b) if b else None,
           'asleep_h': _h(asleep), 'in_bed_h': _h(in_bed), 'awake_h': _h(awake),
           'light_h': _h(stage.get('LIGHT')), 'deep_h': _h(stage.get('DEEP')), 'rem_h': _h(stage.get('REM')),
           'score': None,      # Google's API has no Sleep Score
           'efficiency_pct': round(asleep / in_bed * 100) if asleep is not None and in_bed else None,
           'respiratory_rate': None, 'state': 'pending' if md.get('processed') is False else 'scored'}
    if main:
        out['need_h'] = None   # Fitbit gives no nightly sleep need; Health falls back to hours
    return out


def _n_workout(dp):
    ex = dp.get('exercise') or {}
    iv = ex.get('interval') or {}
    ms = ex.get('metricsSummary') or {}
    a, b = _parse(iv.get('startTime')), _parse(iv.get('endTime'))
    active = _secs(ex.get('activeDuration'))
    zones = ms.get('heartRateZoneDurations') or {}
    zmin = {k: round(_secs(zones[k + 'Time']) / 60, 1) for k in ('light', 'moderate', 'vigorous', 'peak') if _secs(zones.get(k + 'Time')) is not None}
    dist = _num(ms.get('distanceMillimeters', ms.get('distanceMillimiters')), 3)   # the codelab sample spells it "Millimiters"
    kind = str(ex.get('exerciseType') or '')
    elev = _num(ms.get('elevationGainMillimeters'), 3)
    return {'id': str(dp.get('name') or '').rsplit('/', 1)[-1] or None,
            'sport': ex.get('displayName') or (kind.replace('_', ' ').capitalize() if kind else 'Activity'),
            'start': _iso(a) if a else None, 'end': _iso(b) if b else None,
            'minutes': round(active / 60) if active is not None else (round((b - a).total_seconds() / 60) if a and b else None),
            'load': _load(_int(ms.get('activeZoneMinutes'))),
            'avg_hr_bpm': _int(ms.get('averageHeartRateBeatsPerMinute')), 'max_hr_bpm': None,
            'calories_kcal': _num(ms.get('caloriesKcal'), 0), 'energy_kj': None,
            'distance_m': round(dist / 1000) if dist is not None else None,
            'zones_min': None,   # Fitbit's four zones don't map onto zones 0-5; they are in extras.fitbit.zones_min
            'extras': {'fitbit': {'exercise_type': kind or None, 'zones_min': zmin or None, 'steps': _int(ms.get('steps')),
                                  'elevation_gain_m': round(elev / 1000) if elev is not None else None,
                                  'recording': (dp.get('dataSource') or {}).get('recordingMethod')}}}


def _n_day(day, rows):
    """One normalized day record from everything stored for that date."""
    def one(k):
        return (rows.get(k) or [{}])[0]
    sleeps = [r.get('sleep') or {} for r in rows.get('sleep') or []]
    main = next((s for s in sleeps if (s.get('metadata') or {}).get('mainSleep')), None)
    if main is None:
        cands = [s for s in sleeps if not (s.get('metadata') or {}).get('nap')] or sleeps
        main = max(cands, key=_sleep_len) if cands else None
    resp = one('resp').get('dailyRespiratoryRate') or {}
    sleep = None
    if main is not None:
        sleep = _n_sleep(main)
        sleep['respiratory_rate'] = _num(resp.get('breathsPerMinute'))
        sleep['naps'] = [_n_sleep(s, main=False) for s in sorted(sleeps, key=lambda s: (s.get('interval') or {}).get('startTime') or '') if s is not main]
    hrv = one('hrv').get('dailyHeartRateVariability') or {}
    rhr = one('rhr').get('dailyRestingHeartRate') or {}
    spo2 = one('spo2').get('dailyOxygenSaturation') or {}
    temp = one('temp').get('dailySleepTemperatureDerivations') or {}
    azm = one('azm').get('activeZoneMinutes')
    steps = one('steps').get('steps') or {}
    cal = one('calories').get('totalCalories') or {}
    zones = None
    if isinstance(azm, dict):
        zones = {'fat_burn': _int(azm.get('sumInFatBurnHeartZone')) or 0, 'cardio': _int(azm.get('sumInCardioHeartZone')) or 0,
                 'peak': _int(azm.get('sumInPeakHeartZone')) or 0}
        zones['total'] = zones['fat_burn'] + zones['cardio'] + zones['peak']
    nightly, base = _num(temp.get('nightlyTemperatureCelsius'), 2), _num(temp.get('baselineTemperatureCelsius'), 2)
    works = sorted((_n_workout(w) for w in rows.get('exercise') or []), key=lambda w: w['start'] or '')
    md = (main or {}).get('metadata') or {}
    msum = (main or {}).get('summary') or {}
    return {
        'date': day, 'updated_at': None, 'readiness': None,     # no readiness score in Google's API; never invented
        'hrv_ms': _num(hrv.get('averageHeartRateVariabilityMilliseconds')), 'resting_hr_bpm': _int(rhr.get('beatsPerMinute')),
        'spo2_pct': _num(spo2.get('averagePercentage')), 'skin_temp_c': nightly,
        'skin_temp_delta_c': round(nightly - base, 2) if nightly is not None and base is not None else None,
        'respiratory_rate': _num(resp.get('breathsPerMinute')), 'sleep': sleep,
        'load': _load(zones['total'] if zones else None),
        'calories_kcal': _num(cal.get('kcalSum'), 0), 'energy_kj': None,
        'avg_hr_bpm': None, 'max_hr_bpm': None, 'steps': _int(steps.get('countSum')),
        'workouts': works,
        'extras': {'fitbit': {'azm': zones, 'azm_daily_goal': AZM_GOAL, 'readiness_available': False,
                              'hrv_deep_sleep_ms': _num(hrv.get('deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds')),
                              'non_rem_hr_bpm': _int(hrv.get('nonRemHeartRateBeatsPerMinute')), 'hrv_entropy': _num(hrv.get('entropy'), 3),
                              'spo2_range_pct': [_num(spo2.get('lowerBoundPercentage')), _num(spo2.get('upperBoundPercentage'))] if spo2 else None,
                              'skin_temp_baseline_c': base, 'sleep_type': (main or {}).get('type'), 'sleep_stages_status': md.get('stagesStatus'),
                              'minutes_to_fall_asleep': _int(msum.get('minutesToFallAsleep')), 'minutes_after_wake_up': _int(msum.get('minutesAfterWakeUp')),
                              'sleep_manually_edited': md.get('manuallyEdited')}},
    }


def _build_days(dates):
    if not dates:
        return []
    with LOCK:
        c = db()
        try:
            marks = ','.join('?' * len(dates))
            by = {}
            for x in c.execute(f'SELECT kind, day, data FROM records WHERE day IN ({marks}) ORDER BY kind, start, id', list(dates)):
                by.setdefault(x['day'], {}).setdefault(x['kind'], []).append(json.loads(x['data']))
            stamp = {x['day']: x['changed_at'] for x in c.execute(f'SELECT day, changed_at FROM day_marks WHERE day IN ({marks})', list(dates))}
        finally:
            c.close()
    out = []
    for d in dates:
        rec = _n_day(d, by.get(d, {}))
        rec['updated_at'] = stamp.get(d)
        out.append(rec)
    return out


def _token(d):
    return base64.urlsafe_b64encode(json.dumps(d, separators=(',', ':')).encode()).decode().rstrip('=')


def _untoken(s):
    try:
        d = json.loads(base64.urlsafe_b64decode(s + '=' * (-len(s) % 4)))
        if isinstance(d, dict):
            return d
    except (ValueError, TypeError):
        pass
    raise Problem(400, 'page is not a page token from this source.')


def _cursor_seq(s):
    if s in (None, ''):
        return 0
    m = re.fullmatch(r'f1\.(\d{1,15})', str(s))
    if not m:
        raise Problem(400, 'changed_since is not a cursor from this source.')
    return int(m.group(1))


def _seq_now(c):
    row = c.execute("SELECT data FROM meta WHERE key='seq'").fetchone()
    return int(json.loads(row['data'])) if row else 0


def _check_day(v, name):
    if v is None or v == '':
        return None
    if not DATE_RE.match(str(v)):
        raise Problem(400, f'{name} must be YYYY-MM-DD.')
    return str(v)


def contract_days(args):
    """GET /api/days: normalized day records, oldest first, paged; optionally only days changed since a cursor."""
    frm, to = _check_day(args.get('from'), 'from'), _check_day(args.get('to'), 'to')
    if frm and to and frm > to:
        raise Problem(400, 'from must be on or before to.')
    try:
        limit = max(1, min(500, int(args.get('limit') or 100)))
    except (TypeError, ValueError):
        raise Problem(400, 'limit must be a number.') from None
    page = _untoken(str(args['page'])) if args.get('page') else {}
    changed = 'changed_since' in args
    since = _cursor_seq(args.get('changed_since')) if changed else 0
    after = page.get('after') or ''
    with LOCK:
        c = db()
        try:
            hw = int(page.get('hw') or _seq_now(c))
            if changed:
                q, p = ['SELECT day FROM day_marks WHERE changed_seq>? AND changed_seq<=? AND day>?'], [since, hw, after]
            else:
                q, p = ['SELECT DISTINCT day FROM records WHERE day>?'], [after]
            if frm:
                q.append('AND day>=?'); p.append(frm)
            if to:
                q.append('AND day<=?'); p.append(to)
            q.append('ORDER BY day LIMIT ?'); p.append(limit + 1)
            dates = [r['day'] for r in c.execute(' '.join(q), p)]
        finally:
            c.close()
    more = len(dates) > limit
    dates = dates[:limit]
    return {'contract': CONTRACT, 'source': APP_ID, 'dataset': _dataset(), 'days': _build_days(dates),
            'next_page': _token({'after': dates[-1], 'hw': hw}) if more else None, 'cursor': f'f1.{hw}'}


def _counts():
    with LOCK:
        c = db()
        out = {r['kind']: r['n'] for r in c.execute('SELECT kind, COUNT(*) n FROM records GROUP BY kind')}
        span = c.execute('SELECT MIN(day) a, MAX(day) b, COUNT(DISTINCT day) n FROM records').fetchone()
        c.close()
    return out, span['a'], span['b'], span['n']


def status_info():
    k, t, st = _keys(), _tokens(), _state()
    keys = bool(k.get('client_id') and k.get('client_secret'))
    if not (t.get('refresh_token') or t.get('access_token')):
        state = 'needs_login' if keys and st.get('auth') == 'needs_login' else 'not_configured'
    elif st.get('syncing') and _ago(st.get('syncing')) < 7200:
        state = 'syncing'
    elif (st.get('rate_limited_until') or 0) > time.time():
        state = 'rate_limited'
    else:
        state = 'connected'
    counts, first, last, ndays = _counts()
    nxt = None
    if state in ('connected', 'rate_limited') and st.get('last_sync'):
        nxt = _iso(_parse(st['last_sync']) + dt.timedelta(minutes=SYNC_MINUTES))
    live = state in ('connected', 'syncing', 'rate_limited')
    exp = t.get('refresh_expires_at')
    return {
        'contract': CONTRACT, 'source': SOURCE, 'status': state, 'version': VERSION,
        'account': {'name': None} if live or counts else None, 'dataset': _dataset(),
        'last_sync': st.get('last_sync'), 'next_sync': nxt, 'sync_every_minutes': SYNC_MINUTES,
        'history_from': first, 'latest_date': last,
        'counts': {'days': ndays, 'sleeps': counts.get('sleep', 0), 'workouts': counts.get('exercise', 0)},
        'error': st.get('last_error'), 'scales': SCALES,
        'provides': {'readiness': False, 'sleep_score': False, 'sleep_need': False, 'hrv': True, 'resting_hr': True,
                     'load': 'active_zone_minutes'},
        # setup state, for this app's own screen
        'setup': {'keys_saved': keys, 'keys_verified': k.get('verified'), 'redirect_uri': k.get('redirect_uri') if keys else None,
                  'backfilled': bool(st.get('backfilled')), 'sync_kind': st.get('sync_kind') if state == 'syncing' else None,
                  'progress': st.get('progress') if state == 'syncing' else None,
                  'missing_scopes': st.get('missing_scopes') if live else None,
                  'signin_ends': _iso(dt.datetime.fromtimestamp(exp, dt.timezone.utc)) if live and exp else None},
    }


def disconnect(forget=False):
    t = _tokens()
    tok = t.get('refresh_token') or t.get('access_token')
    if tok:
        try:   # best effort: tell Google to end this app's access
            _http('POST', REVOKE_URL, form={'token': tok}, timeout=15)
        except GoogleError:
            pass
    with LOCK:
        _save_tokens(None)
        for p in list(DATA.glob('fitbit.sqlite3*')):
            p.unlink(missing_ok=True)
        _shred('state.json')     # also drops the dataset id, so readers know the copy is gone
        if forget:
            _shred('client.json')
    return status_info()


# ---------------- MCP ----------------

_NONE = {'type': 'object', 'properties': {}, 'additionalProperties': False}
TOOLS = [
    {'name': 'health_data_status', 'description': "Fitbit connector status (contract wizard.health-data/1; data via Google's Health API): not_configured, needs_login, syncing, connected or rate_limited; last/next sync, history span, record counts and missing permissions. Never returns keys or tokens. This source has no readiness score (Google's API does not provide Fitbit's Daily Readiness) and measures activity in Active Zone Minutes. If not connected, send the owner the Fitbit app link [Fitbit](/wizard-fitbit/); its own screen walks him through setup. Never ask for Google keys, passwords or codes in chat. For health questions use the Health app's tools instead.",
     'inputSchema': _NONE, 'annotations': {'readOnlyHint': True}},
    {'name': 'health_data_sync', 'description': 'Ask the Fitbit connector to pull new data from Google now (it also syncs every 20 minutes). Returns at once; check health_data_status for progress. Reads only.',
     'inputSchema': _NONE, 'annotations': {'readOnlyHint': False, 'destructiveHint': False, 'idempotentHint': True}},
    {'name': 'health_data_days', 'description': 'Raw contract read: normalized Fitbit day records (HRV, resting HR, SpO2, breathing rate, skin temperature, sleep and stages, Active Zone Minutes, steps, calories, workouts, extras.fitbit; readiness is always null), oldest first, paged with next_page; pass changed_since=<cursor> to get only changed days. For owner questions prefer the Health app tools.',
     'inputSchema': {'type': 'object', 'properties': {
         'from': {'type': 'string', 'description': 'YYYY-MM-DD inclusive'}, 'to': {'type': 'string', 'description': 'YYYY-MM-DD inclusive'},
         'limit': {'type': 'integer', 'description': 'days per page, default 100, max 500'},
         'page': {'type': 'string', 'description': 'next_page from the previous reply'},
         'changed_since': {'type': 'string', 'description': 'cursor from an earlier reply; empty string = everything'}},
         'additionalProperties': False}, 'annotations': {'readOnlyHint': True}},
]


def request_sync(full=False):
    s = status_info()
    if s['status'] in ('not_configured', 'needs_login'):
        raise Problem(409, 'Fitbit is not connected. The owner finishes setup in the Fitbit app (/wizard-fitbit/).')
    start_sync_thread(full=full)
    return dict(status_info(), note='Sync started.')


def call(name, a):
    if name == 'health_data_status':
        return status_info()
    if name == 'health_data_sync':
        return request_sync()
    if name == 'health_data_days':
        return contract_days(a)
    raise KeyError(name)


def rpc(msg):
    if not isinstance(msg, dict):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Expected a JSON-RPC object.'}}
    m, i, p = msg.get('method'), msg.get('id'), msg.get('params') or {}
    if 'id' not in msg:
        return None
    try:
        if m == 'initialize':
            r = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': APP_ID, 'version': VERSION}}
        elif m == 'tools/list':
            r = {'tools': TOOLS}
        elif m == 'tools/call':
            try:
                v = call(p.get('name'), p.get('arguments') or {})
            except KeyError:
                return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32602, 'message': 'Unknown tool.'}}
            r = {'content': [{'type': 'text', 'text': json.dumps(v, ensure_ascii=False)}], 'structuredContent': v, 'isError': False}
        elif m == 'ping':
            r = {}
        else:
            return {'jsonrpc': '2.0', 'id': i, 'error': {'code': -32601, 'message': 'Unknown method.'}}
    except (Problem, GoogleError) as e:
        r = {'content': [{'type': 'text', 'text': e.message}], 'isError': True}
    except (ValueError, TypeError, AttributeError):
        r = {'content': [{'type': 'text', 'text': 'Invalid arguments.'}], 'isError': True}
    return {'jsonrpc': '2.0', 'id': i, 'result': r}


# ---------------- HTTP ----------------

def _csv(name):
    return {x.strip().lower() for x in os.environ.get(name, '').split(',') if x.strip()}


_LOOPBACK = {f'127.0.0.1:{PORT}', f'localhost:{PORT}', f'{APP_ID}:{PORT}'}
TRUSTED_HOSTS = _csv('WIZARD_TRUSTED_HOSTS') | _LOOPBACK
TRUSTED_ORIGINS = _csv('WIZARD_TRUSTED_ORIGINS') | {'http://' + h for h in _LOOPBACK}
# A bare host name (no port) in WIZARD_TRUSTED_HOSTS is trusted on every shell port of that host.
TRUSTED_NAMES = {h.split(':')[0] for h in TRUSTED_HOSTS}
CSP = ("default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
       "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")


def _host_ok(host):
    host = (host or '').lower()
    return host in TRUSTED_HOSTS or host.split(':')[0] in TRUSTED_NAMES


class Handler(BaseHTTPRequestHandler):
    def refused(self, write=False):
        host = (self.headers.get('Host') or '').lower()
        if not _host_ok(host):
            self.send(403, {'error': 'Unknown host.'}); return True
        origin = (self.headers.get('Origin') or '').lower()
        if origin and origin not in TRUSTED_ORIGINS and urlsplit(origin).netloc != host:
            self.send(403, {'error': 'Cross-origin request refused.'}); return True
        if write and not origin and (self.headers.get('Sec-Fetch-Site') or '') == 'cross-site':
            self.send(403, {'error': 'Cross-origin request refused.'}); return True
        return False

    def forwarded(self):
        return any(self.headers.get(h) for h in FORWARDED)

    def send(self, code, body=None, kind='application/json', extra=None):
        b = body if isinstance(body, bytes) else b'' if body is None else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(b)

    def route(self):
        p = urlsplit(self.path).path
        return '/' if p == PREFIX else p[len(PREFIX):] if p.startswith(PREFIX + '/') else p

    def body(self):
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise Problem(415, 'Use application/json.')
        size = int(self.headers.get('Content-Length', '0') or 0)
        if not 0 <= size <= 65536:
            raise Problem(413, 'Request too large.')
        try:
            d = json.loads(self.rfile.read(size) or b'{}')
        except ValueError:
            raise Problem(400, 'Invalid JSON.') from None
        if not isinstance(d, dict):
            raise Problem(400, 'Expected a JSON object.')
        return d

    def callback_page(self, ok, message):
        home = PREFIX + '/' if self.path.startswith(PREFIX) else '/'
        title = 'Fitbit connected' if ok else 'Fitbit sign-in did not finish'
        esc = lambda s: s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        html = (f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<title>{title}</title><body style="font:1rem/1.5 system-ui;margin:2rem;max-width:36rem">'
                f'<h1 style="font-size:1.4rem">{title}</h1><p>{esc(message)}</p><p><a href="{home}">Open Fitbit</a></p>')
        self.send(200 if ok else 400, html.encode(), 'text/html; charset=utf-8')

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.refused():
            return
        path = self.route()
        query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query, keep_blank_values=True).items()}
        try:
            if path in ('/healthz', '/api/health'):
                s = status_info()
                return self.send(200, {'ok': True, 'app': APP_ID, 'version': VERSION, 'status': s['status'],
                                       'connected': s['status'] in ('connected', 'syncing', 'rate_limited'), 'last_sync': s['last_sync']})
            if path == '/api/status':
                return self.send(200, status_info())
            if path == '/api/setup':
                return self.send(200, setup_info())
            if path == '/api/days':
                if self.forwarded():
                    return self.send(403, {'error': 'Health data reads are box-only.'})
                return self.send(200, contract_days(query))
            if path in ('/callback', '/oauth/callback'):   # when the Redirect URL points at this app itself
                try:
                    login_finish('http://x' + self.path)
                    return self.callback_page(True, 'Your Fitbit data is downloading now. You can close this tab and go back to the Fitbit app.')
                except (Problem, GoogleError) as e:
                    return self.callback_page(False, e.message)
            if path == '/openapi.json':
                return self.send(200, (ROOT / 'openapi.json').read_bytes())
            name = 'index.html' if path == '/' else path[1:]
            kind = STATIC.get(Path(name).suffix)
            if kind and '/' not in name and not name.startswith('.') and (ROOT / 'web' / name).is_file():
                return self.send(200, (ROOT / 'web' / name).read_bytes(), kind)
            self.send(405 if path == '/mcp' else 404, {'error': 'Use POST /mcp.' if path == '/mcp' else 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid query.'})

    def do_POST(self):
        if self.refused(write=True):
            return
        path = self.route()
        try:
            if path == '/mcp':
                if self.forwarded():
                    return self.send(403, {'error': 'MCP is box-only.'})
                out = rpc(self.body())
                return self.send(202 if out is None else 200, out)
            b = self.body()
            if path == '/api/keys':
                return self.send(200, save_keys(b))
            if path == '/api/keys/test':
                return self.send(200, test_keys())
            if path == '/api/keys/forget':
                return self.send(200, forget_keys())
            if path == '/api/oauth/start':
                return self.send(200, login_start())
            if path == '/api/oauth/finish':
                return self.send(200, login_finish(b.get('url')))
            if path == '/api/sync':
                return self.send(202, request_sync(full=bool(b.get('full'))))
            if path == '/api/disconnect':
                return self.send(200, disconnect(forget=bool(b.get('forget_keys'))))
            self.send(404, {'error': 'Not found.'})
        except Problem as e:
            self.send(e.code, {'error': e.message})
        except GoogleError as e:
            self.send(429 if e.code == 'rate_limited' else 502, {'error': e.message})
        except (ValueError, TypeError):
            self.send(400, {'error': 'Invalid request.'})

    def log_message(self, *a):
        pass


def main():
    secure_files()
    st = _state()
    if st.get('syncing'):
        _update_state(syncing=None, progress=None, sync_kind=None)   # a restart interrupted it; the scheduler resumes
    threading.Thread(target=_scheduler, daemon=True).start()
    ThreadingHTTPServer((os.environ.get('BIND', '0.0.0.0'), int(PORT)), Handler).serve_forever()

# ---- wizard.app-switches/1 ------------------------------------------------------------------------------------------
# Owner feature switches for the Tracker's Try it (wizard-os docs/APP_SWITCHES.md). Shared block: the same text ships in
# every Wizard app (template: wizard-app-manager templates/hello-wizard). Every new feature ships behind a switch, off by
# default: append it to SWITCHES with the issue it delivers, e.g.
#   {'id': 'compact-header', 'name': 'Compact top bar', 'description': '...', 'default': False,
#    'issues': ['humanitylabs-org/<repo>#20']}
# and gate the feature on switch_on('compact-header'). State lives on the box (DATA_DIR/switches.json), so the app's
# page, the Tracker (through the App Manager) and the agent see one state. GET/POST /api/switches, owner only: the
# box's Tailscale Serve owner login with X-Wizard-Switch: 1; never a bearer, agent grant or box-local host.
import json as _sw_json
import os as _sw_os
import re as _sw_re
import threading as _sw_threading
from pathlib import Path as _SwPath
from urllib.parse import urlsplit as _sw_urlsplit

SWITCHES = []
_SW_CONTRACT = 'wizard.app-switches/1'
_SW_ROUTE = _sw_re.compile(r'(?:/[a-z0-9][a-z0-9-]{0,62})?/api/switches/?')
_SW_LOCAL = {'localhost', '127.0.0.1', '::1', '[::1]', '0.0.0.0'}
_sw_lock = _sw_threading.Lock()


def _sw_file():
    return _SwPath(_sw_os.environ.get('WIZARD_SWITCHES_DIR') or _sw_os.environ.get('DATA_DIR') or '/data') / 'switches.json'


def _sw_saved():
    try:
        saved = _sw_json.loads(_sw_file().read_text())
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def switches_doc():
    saved = _sw_saved()
    return {'contract': _SW_CONTRACT, 'switches': [
        dict(s, on=saved[s['id']] if isinstance(saved.get(s['id']), bool) else bool(s.get('default'))) for s in SWITCHES]}


def switch_on(switch_id):
    return next((s['on'] for s in switches_doc()['switches'] if s['id'] == switch_id), False)


def _sw_set(switch_id, on):
    with _sw_lock:
        ids = {s['id'] for s in SWITCHES}
        saved = {k: v for k, v in _sw_saved().items() if k in ids and isinstance(v, bool)}
        saved[switch_id] = on
        f = _sw_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + '.tmp')
        tmp.write_text(_sw_json.dumps(saved))
        _sw_os.replace(tmp, f)
    return switches_doc()


def _sw_owner(h, write):
    hd = h.headers
    if hd.get('Authorization') or hd.get('X-Wizard-Switch') != '1':
        return False
    if (hd.get('Sec-Fetch-Site') or 'same-origin').lower() not in ('same-origin', 'none'):
        return False
    host = (hd.get('Host') or '').strip().lower()
    name = host.split(']')[0] + ']' if host.startswith('[') else host.rsplit(':', 1)[0]
    if not name or name in _SW_LOCAL or '.' not in name:      # box-local and container hosts never prove the owner
        return False
    origin = hd.get('Origin')
    trusted = {o.strip() for o in (_sw_os.environ.get('WIZARD_TRUSTED_ORIGINS') or '').split(',') if o.strip()}
    if origin and _sw_urlsplit(origin).netloc.lower() != host and origin not in trusted:
        return False
    if write and not origin:
        return False
    logins = [x.strip() for x in (hd.get_all('Tailscale-User-Login') or [])]
    owners = {x.strip().lower() for x in (_sw_os.environ.get('WIZARD_OWNER_LOGINS') or '').split(',') if x.strip()}
    return len(logins) == 1 and '@' in logins[0] and (not owners or logins[0].lower() in owners)


def _sw_send(h, code, doc):
    body = _sw_json.dumps(doc).encode()
    h.send_response(code)
    h.send_header('Content-Type', 'application/json')
    h.send_header('Cache-Control', 'no-store')
    h.send_header('Content-Length', str(len(body)))
    h.end_headers()
    if h.command != 'HEAD':
        h.wfile.write(body)


def _sw_handle(h):
    """Serve /api/switches (or /<app>/api/switches) and return True; False for every other request."""
    if not _SW_ROUTE.fullmatch(_sw_urlsplit(h.path).path):
        return False
    if h.command in ('GET', 'HEAD'):
        if not _sw_owner(h, False):
            _sw_send(h, 403, {'error': "Only this Wizard's owner can see these switches.", 'code': 'owner_only'})
        else:
            _sw_send(h, 200, switches_doc())
        return True
    if not _sw_owner(h, True):
        _sw_send(h, 403, {'error': "Only this Wizard's owner can change these switches.", 'code': 'owner_only'})
        return True
    try:
        if (h.headers.get('Content-Type') or '').split(';')[0].strip() != 'application/json':
            raise ValueError
        size = int(h.headers.get('Content-Length') or '0')
        if size <= 0 or size > 4096:
            raise ValueError
        body = _sw_json.loads(h.rfile.read(size))
        if not isinstance(body, dict) or not isinstance(body.get('id'), str) or not isinstance(body.get('on'), bool) \
                or set(body) - {'id', 'on'}:
            raise ValueError
    except (ValueError, TypeError):
        _sw_send(h, 400, {'error': 'Send JSON {"id": "...", "on": true|false}', 'code': 'invalid_input'})
        return True
    if body['id'] not in {s['id'] for s in SWITCHES}:
        _sw_send(h, 404, {'error': 'No such switch', 'code': 'unknown_switch'})
        return True
    try:
        _sw_send(h, 200, _sw_set(body['id'], body['on']))
    except OSError:
        _sw_send(h, 500, {'error': 'storage_failure'})
    return True


def wizard_switches_install(handler_cls):
    """Route the switch endpoint ahead of the app's own GET/HEAD/POST handlers."""
    for method in ('do_GET', 'do_HEAD', 'do_POST'):
        orig = getattr(handler_cls, method, None)
        if getattr(orig, '_wizard_switches', False):
            continue

        def wrapped(self, _orig=orig):
            if _sw_handle(self):
                return None
            if _orig is None:
                return self.send_error(501)
            return _orig(self)
        wrapped._wizard_switches = True
        setattr(handler_cls, method, wrapped)
    return handler_cls
# ---- end wizard.app-switches/1 ---------------------------------------------------------------------------------------

wizard_switches_install(Handler)


if __name__ == '__main__':
    main()
