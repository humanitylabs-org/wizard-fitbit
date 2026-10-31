"""Tests for the Fitbit connector against a fake Google (OAuth token/revoke endpoints + Google Health API v4), built
from Google's published docs: https://developers.google.com/health (data types, list/dailyRollUp methods, filters,
error catalog, codelab samples). Covers setup keys, the key test, OAuth state + PKCE, refresh with and without
rotation, the 7-day Testing-mode sign-in, paging, 401/429/403 handling, partial consent, incremental overlap,
deletions, the wizard.health-data/1 contract (normalized days, paging, changed_since, dataset id, box-only reads,
no invented readiness, Active Zone Minutes load scale), HTTP/MCP guards and disconnect.
Run: python3 -m unittest discover -s tests          (live Test-button check: FITBIT_LIVE_TEST=1)"""
import base64
import copy
import datetime as dt
import hashlib
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
CID, SECRET = '123456789012-testclientabcdef0123456789.apps.googleusercontent.com', 'GOCSPX-test-secret-value-XYZ'
SCOPES = ['https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly',
          'https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly',
          'https://www.googleapis.com/auth/googlehealth.sleep.readonly']
DAYS = 40
OFF = 25200            # +07:00, as Google sends it: "25200s"
TODAY = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=OFF)).date()
DAY0 = TODAY - dt.timedelta(days=DAYS)


def z(t):
    return t.strftime('%Y-%m-%dT%H:%M:%SZ')


def gdate(d):
    return {'year': d.year, 'month': d.month, 'day': d.day}


def local(d, h, m=0):
    """UTC instant of local time d h:m at +07:00."""
    return dt.datetime(d.year, d.month, d.day, h, m, tzinfo=dt.timezone.utc) - dt.timedelta(seconds=OFF)


def sleep_point(i, d, nap=False):
    """Shape from the Google Health API sleep guide example plus the reference's stagesSummary/metadata."""
    if nap:
        s, e = local(d, 14), local(d, 14, 40)
        stages, summ, md = [], {'minutesInSleepPeriod': '40', 'minutesAsleep': '35', 'minutesAwake': '5'}, {'nap': True, 'mainSleep': False, 'processed': True}
        kind = 'CLASSIC'
    else:
        s, e = local(d - dt.timedelta(days=1), 22, 30), local(d, 6, 30)
        stages = [{'startTime': z(s), 'startUtcOffset': f'{OFF}s', 'endTime': z(s + dt.timedelta(minutes=75)), 'endUtcOffset': f'{OFF}s', 'type': 'LIGHT'},
                  {'startTime': z(s + dt.timedelta(minutes=75)), 'startUtcOffset': f'{OFF}s', 'endTime': z(s + dt.timedelta(minutes=165)), 'endUtcOffset': f'{OFF}s', 'type': 'DEEP'}]
        summ = {'minutesInSleepPeriod': '480', 'minutesAsleep': '455', 'minutesAwake': '25', 'minutesToFallAsleep': '15', 'minutesAfterWakeUp': '10',
                'stagesSummary': [{'type': 'LIGHT', 'minutes': '250', 'count': '14'}, {'type': 'DEEP', 'minutes': str(90 + i % 5), 'count': '4'},
                                  {'type': 'REM', 'minutes': str(115 - i % 5), 'count': '5'}, {'type': 'AWAKE', 'minutes': '25', 'count': '12'}]}
        md = {'stagesStatus': 'STAGES_STATE_UNSPECIFIED', 'processed': True, 'nap': False, 'manuallyEdited': False, 'mainSleep': True}
        kind = 'STAGES'
    iv = {'startTime': z(s), 'startUtcOffset': f'{OFF}s', 'endTime': z(e), 'endUtcOffset': f'{OFF}s'}
    if i % 2 == 0:            # civil times are output-only; Google may send them; test both shapes
        ld = (e + dt.timedelta(seconds=OFF))
        iv['civilEndTime'] = {'date': gdate(ld.date()), 'time': {'hours': ld.hour, 'minutes': ld.minute}}
    return {'name': f'users/1234567/dataTypes/sleep/dataPoints/{"n" if nap else "s"}{i:05d}',
            'dataSource': {'recordingMethod': 'PASSIVELY_MEASURED', 'platform': 'FITBIT'},
            'sleep': {'interval': iv, 'type': kind, 'stages': stages,
                      'shortAwakenings': [{'startTime': z(s + dt.timedelta(minutes=40)), 'startUtcOffset': f'{OFF}s',
                                           'endTime': z(s + dt.timedelta(minutes=41, seconds=30)), 'endUtcOffset': f'{OFF}s', 'type': 'AWAKE'}] if not nap else [],
                      'summary': summ, 'metadata': md, 'createTime': z(e), 'updateTime': z(e)}}


def exercise_point(i, d):
    """The codelab's sample exercise (including its 'distanceMillimiters' spelling), moved to day d."""
    s = local(d, 18, 10)
    return {'name': f'users/1234567/dataTypes/exercise/dataPoints/{8896720705097069096 + i}',
            'dataSource': {'recordingMethod': 'MANUAL', 'platform': 'FITBIT'},
            'exercise': {'interval': {'startTime': z(s), 'startUtcOffset': f'{OFF}s', 'endTime': z(s + dt.timedelta(minutes=15)), 'endUtcOffset': f'{OFF}s'},
                         'exerciseType': 'WALKING',
                         'metricsSummary': {'caloriesKcal': 17, 'distanceMillimiters': 1609344, 'steps': '2038', 'averagePaceSecondsPerMeter': 0.55923407301360051,
                                            'averageHeartRateBeatsPerMinute': '81', 'activeZoneMinutes': '0', 'heartRateZoneDurations': {'lightTime': '900s'}},
                         'exerciseMetadata': {}, 'displayName': 'Walk', 'activeDuration': '900s',
                         'exerciseEvents': [{'eventTime': z(s), 'eventUtcOffset': f'{OFF}s', 'exerciseEventType': 'START'},
                                            {'eventTime': z(s + dt.timedelta(minutes=15)), 'eventUtcOffset': f'{OFF}s', 'exerciseEventType': 'STOP'}],
                         'updateTime': z(s + dt.timedelta(hours=2))}}


def make_data():
    sleep, exercise, hrv, rhr, spo2, resp, temp, azm, steps, cal = [], [], [], [], [], [], [], [], [], []
    for i in range(DAYS):
        d = DAY0 + dt.timedelta(days=i + 1)          # DAY0+1 .. TODAY
        if d < TODAY:
            sleep.append(sleep_point(i, d))
            if i % 5 == 0:
                sleep.append(sleep_point(i, d, nap=True))
            hrv.append({'name': f'users/1234567/dataTypes/daily-heart-rate-variability/dataPoints/h{i}', 'dataSource': {'recordingMethod': 'DERIVED'},
                        'dailyHeartRateVariability': {'date': gdate(d), 'averageHeartRateVariabilityMilliseconds': 41.25 + i % 7,
                                                      'nonRemHeartRateBeatsPerMinute': '58', 'entropy': 2.871, 'deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds': 45.5}})
            rhr.append({'name': f'users/1234567/dataTypes/daily-resting-heart-rate/dataPoints/r{i}',
                        'dailyRestingHeartRate': {'date': gdate(d), 'dailyRestingHeartRateMetadata': {'calculationMethod': 'WITH_SLEEP'}, 'beatsPerMinute': str(55 + i % 4)}})
            spo2.append({'name': f'users/1234567/dataTypes/daily-oxygen-saturation/dataPoints/o{i}',
                         'dailyOxygenSaturation': {'date': gdate(d), 'averagePercentage': 96.4, 'lowerBoundPercentage': 94.1, 'upperBoundPercentage': 98.7}})
            resp.append({'name': f'users/1234567/dataTypes/daily-respiratory-rate/dataPoints/b{i}', 'dailyRespiratoryRate': {'date': gdate(d), 'breathsPerMinute': 14.8}})
            temp.append({'name': f'users/1234567/dataTypes/daily-sleep-temperature-derivations/dataPoints/t{i}',
                         'dailySleepTemperatureDerivations': {'date': gdate(d), 'nightlyTemperatureCelsius': 33.9, 'baselineTemperatureCelsius': 33.6}})
        if i % 3 == 0:
            exercise.append(exercise_point(i, d))
        if i % 7 != 6:                               # one day a week the watch was off: Google leaves the value unset
            azm.append((d, {'sumInCardioHeartZone': str(2 * (i % 9)), 'sumInPeakHeartZone': str(2 * (i % 2)), 'sumInFatBurnHeartZone': str(10 + i % 6)}))
            steps.append((d, {'countSum': '3822' if i == 0 else str(6000 + 37 * i)}))
            cal.append((d, {'kcalSum': 2210.6 + i}))
    return {'sleep': sleep, 'exercise': exercise, 'daily-heart-rate-variability': hrv, 'daily-resting-heart-rate': rhr,
            'daily-oxygen-saturation': spo2, 'daily-respiratory-rate': resp, 'daily-sleep-temperature-derivations': temp,
            'roll:active-zone-minutes': azm, 'roll:steps': steps, 'roll:total-calories': cal}


ROLL_FIELD = {'active-zone-minutes': 'activeZoneMinutes', 'steps': 'steps', 'total-calories': 'totalCalories'}
ROLL_MAX = {'total-calories': 14}
TYPE_SCOPE = {'sleep': 'sleep.readonly', 'exercise': 'activity_and_fitness.readonly'}


class Fake:
    def __init__(self):
        self.data = make_data()
        self.access, self.refresh = None, None
        self.used_refresh = set()
        self.challenges = {}
        self.calls = []
        self.force_401 = self.force_429 = 0
        self.rotate = False
        self.granted = list(SCOPES)
        self.revoked = []
        self.refresh_expires = 604799        # Testing-mode project: 7 days


FAKE = Fake()


def google_error(code, status, reason, message):
    return {'error': {'code': code, 'message': message, 'status': status,
                      'details': [{'@type': 'type.googleapis.com/google.rpc.ErrorInfo', 'reason': reason, 'domain': 'health.googleapis.com'}]}}


class FakeGoogle(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body, headers=None):
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=UTF-8')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def issue(self, with_refresh=True):
        FAKE.access = 'ya29.' + os.urandom(8).hex()
        out = {'access_token': FAKE.access, 'expires_in': 3599, 'scope': ' '.join(FAKE.granted), 'token_type': 'Bearer'}
        if with_refresh:
            FAKE.refresh = '1//' + os.urandom(8).hex()
            out['refresh_token'] = FAKE.refresh
            if FAKE.refresh_expires:
                out['refresh_token_expires_in'] = FAKE.refresh_expires
        return out

    def do_POST(self):
        u = urlsplit(self.path)
        raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        if u.path == '/token':
            f = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
            FAKE.calls.append(('POST', '/token', f.get('grant_type')))
            if f.get('client_id') != CID:
                return self.reply(401, {'error': 'invalid_client', 'error_description': 'The OAuth client was not found.'})
            if f.get('client_secret') != SECRET:
                return self.reply(401, {'error': 'invalid_client', 'error_description': 'Unauthorized'})
            if f.get('grant_type') == 'authorization_code':
                ch = FAKE.challenges.get(f.get('code'))
                ok = ch and base64.urlsafe_b64encode(hashlib.sha256(f.get('code_verifier', '').encode()).digest()).decode().rstrip('=') == ch
                if ok and f.get('redirect_uri') == 'http://localhost:3000/callback':
                    FAKE.challenges.pop(f['code'])
                    return self.reply(200, self.issue())
                return self.reply(400, {'error': 'invalid_grant', 'error_description': 'Malformed auth code.'})
            if f.get('grant_type') == 'refresh_token':
                if f.get('refresh_token') == FAKE.refresh and f['refresh_token'] not in FAKE.used_refresh:
                    if FAKE.rotate:
                        FAKE.used_refresh.add(f['refresh_token'])
                        return self.reply(200, self.issue())
                    return self.reply(200, self.issue(with_refresh=False))
                return self.reply(400, {'error': 'invalid_grant', 'error_description': 'Token has been expired or revoked.'})
            return self.reply(400, {'error': 'unsupported_grant_type'})
        if u.path == '/revoke':
            FAKE.revoked.append(parse_qs(raw.decode()).get('token', [''])[0])
            return self.reply(200, {})
        m = re.fullmatch(r'/v4/users/me/dataTypes/([a-z-]+)/dataPoints:dailyRollUp', u.path)
        if m:
            return self.rollup(m.group(1), json.loads(raw or b'{}'))
        return self.reply(404, google_error(404, 'NOT_FOUND', 'RESOURCE_NOT_FOUND', 'Not found'))

    def guard(self, dtype):
        if self.headers.get('Authorization') != 'Bearer ' + str(FAKE.access):
            self.reply(401, google_error(401, 'UNAUTHENTICATED', 'UNAUTHENTICATED', 'Request had invalid authentication credentials.')); return True
        if FAKE.force_401:
            FAKE.force_401 -= 1
            self.reply(401, google_error(401, 'UNAUTHENTICATED', 'UNAUTHENTICATED', 'Request had invalid authentication credentials.')); return True
        if FAKE.force_429:
            FAKE.force_429 -= 1
            self.reply(429, google_error(429, 'RESOURCE_EXHAUSTED', 'RESOURCE_EXHAUSTED', 'Quota exceeded or rate limited.'), {'Retry-After': '0'}); return True
        need = 'https://www.googleapis.com/auth/googlehealth.' + TYPE_SCOPE.get(dtype, 'activity_and_fitness.readonly' if dtype in ROLL_FIELD else 'health_metrics_and_measurements.readonly')
        if need not in FAKE.granted:
            self.reply(403, google_error(403, 'PERMISSION_DENIED', 'MISSING_OAUTH_SCOPE', 'Required OAuth scope(s) are missing for this operation.')); return True
        return False

    def rollup(self, dtype, body):
        FAKE.calls.append(('POST', dtype, body))
        if self.guard(dtype):
            return
        r = body.get('range') or {}
        try:
            a = dt.date(**r['start']['date']); b = dt.date(**r['end']['date'])
        except (KeyError, TypeError):
            return self.reply(400, google_error(400, 'INVALID_ARGUMENT', 'INVALID_CIVIL_DATE_TIME', 'Invalid civil date time.'))
        mx = ROLL_MAX.get(dtype, 90)
        if (b - a).days > mx or int(body.get('pageSize') or 0) > mx:
            return self.reply(400, google_error(400, 'INVALID_ARGUMENT', 'INVALID_ROLLUP_QUERY_DURATION', f'The duration covered by window_size_days * page_size must not exceed {mx} days for {dtype}.'))
        out = []
        for d, val in FAKE.data['roll:' + dtype]:
            if a <= d < b:
                out.append({'civilStartTime': {'date': gdate(d), 'time': {}}, 'civilEndTime': {'date': gdate(d + dt.timedelta(days=1)), 'time': {}},
                            ROLL_FIELD[dtype]: val})
        out.sort(key=lambda x: (x['civilStartTime']['date']['year'], x['civilStartTime']['date']['month'], x['civilStartTime']['date']['day']), reverse=True)
        self.reply(200, {'rollupDataPoints': out})

    def do_GET(self):
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        m = re.fullmatch(r'/v4/users/me/dataTypes/([a-z-]+)/dataPoints', u.path)
        if not m:
            return self.reply(404, google_error(404, 'NOT_FOUND', 'RESOURCE_NOT_FOUND', 'Not found'))
        dtype = m.group(1)
        FAKE.calls.append(('GET', dtype, q))
        if self.guard(dtype):
            return
        size = int(q.get('pageSize') or (25 if dtype in ('sleep', 'exercise') else 1440))
        if dtype in ('sleep', 'exercise') and size > 25:
            return self.reply(400, google_error(400, 'INVALID_ARGUMENT', 'INVALID_ARGUMENT', 'The maximum page size for exercise and sleep is 25.'))
        flt = q.get('filter', '')
        member = {'sleep': 'sleep.interval.civil_end_time', 'exercise': 'exercise.interval.civil_start_time'}.get(dtype, dtype.replace('-', '_') + '.date')
        mm = re.fullmatch(re.escape(member) + r' >= "(\d{4}-\d{2}-\d{2})" AND ' + re.escape(member) + r' < "(\d{4}-\d{2}-\d{2})"', flt)
        if not mm:
            return self.reply(400, google_error(400, 'INVALID_ARGUMENT', 'INVALID_DATA_POINT_FILTER', 'Invalid filter.'))
        lo, hi = mm.groups()
        rows = []
        for dp in FAKE.data[dtype]:
            if dtype in ('sleep', 'exercise'):
                iv = dp[dtype]['interval']
                t = dt.datetime.fromisoformat((iv['endTime'] if dtype == 'sleep' else iv['startTime']).replace('Z', '+00:00')) + dt.timedelta(seconds=OFF)
                key = t.date().isoformat()
            else:
                body = next(v for k, v in dp.items() if k.startswith('daily'))
                key = dt.date(**body['date']).isoformat()
            if lo <= key < hi:
                rows.append((key, dp))
        rows.sort(key=lambda x: x[0], reverse=True)                # descending by start, as documented
        off = int(q.get('pageToken') or 0)
        page = [dp for _, dp in rows[off:off + size]]
        out = {'dataPoints': page}
        if off + size < len(rows):
            out['nextPageToken'] = str(off + size)
        self.reply(200, out)


def http(port, method, path, body=None, headers=None):
    h = {'Host': f'127.0.0.1:{port}'}
    if body is not None:
        h['Content-Type'] = 'application/json'
    h.update(headers or {})
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class FitbitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = ThreadingHTTPServer(('127.0.0.1', 0), FakeGoogle)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        fp = cls.fake.server_address[1]
        cls.tmp = tempfile.mkdtemp()
        os.environ.update({'DATA_DIR': cls.tmp + '/private', 'FITBIT_TOKEN_URL': f'http://127.0.0.1:{fp}/token',
                           'FITBIT_REVOKE_URL': f'http://127.0.0.1:{fp}/revoke', 'FITBIT_API_BASE': f'http://127.0.0.1:{fp}/v4',
                           'FITBIT_REQUEST_GAP': '0', 'FITBIT_BACKOFF': '0.01', 'FITBIT_HISTORY_DAYS': '60',
                           'PORT': '18998', 'WIZARD_TRUSTED_HOSTS': 'box.example.ts.net'})
        sys.path.insert(0, str(ROOT / 'app'))
        import server
        cls.S = importlib.reload(server)
        cls.app = ThreadingHTTPServer(('127.0.0.1', 0), cls.S.Handler)
        threading.Thread(target=cls.app.serve_forever, daemon=True).start()
        cls.port = cls.app.server_address[1]
        cls.S.TRUSTED_HOSTS.add(f'127.0.0.1:{cls.port}')

    @classmethod
    def tearDownClass(cls):
        cls.fake.shutdown()
        cls.app.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def files_text(self):
        return ''.join(p.read_bytes().decode('latin-1') for p in Path(self.tmp).rglob('*') if p.is_file())

    def wait_sync(self, timeout=30):
        t = time.time()
        time.sleep(0.05)
        while time.time() - t < timeout:
            if not self.S.SYNC_LOCK.locked() and self.S._state().get('last_sync') and not self.S._state().get('syncing'):
                return
            time.sleep(0.05)
        self.fail('sync did not finish')

    def connect(self, code='good-code'):
        S = self.S
        start = S.login_start()
        q = {k: v[0] for k, v in parse_qs(urlsplit(start['authorize_url']).query).items()}
        FAKE.challenges[code] = q['code_challenge']           # what Google remembers from the consent screen
        return S.login_finish(f"http://localhost:3000/callback?state={q['state']}&code={code}&scope={'+'.join(FAKE.granted)}"), q

    def test_flow(self):
        S = self.S
        # ---- from scratch
        self.assertEqual(S.status_info()['status'], 'not_configured')
        info = S.setup_info()
        self.assertEqual(info['scopes'], SCOPES)
        self.assertEqual(info['default_redirect'], 'http://localhost:3000/callback')
        self.assertIn('health.googleapis.com', info['links']['enable_api'])
        with self.assertRaises(S.Problem):
            S.login_start()

        # ---- step 4: keys through the HTTP API the form uses; the secret is never echoed
        code, body = http(self.port, 'POST', '/api/keys', {'client_id': 'not-a-google-client', 'client_secret': SECRET, 'redirect_uri': 'http://localhost:3000/callback'})
        self.assertEqual(code, 400)
        self.assertIn('googleusercontent', json.loads(body)['error'])
        code, body = http(self.port, 'POST', '/api/keys', {'client_id': CID, 'client_secret': 'GOCSPX-wrong-secret', 'redirect_uri': 'http://localhost:3000/callback'})
        self.assertEqual(code, 200)
        r = json.loads(http(self.port, 'POST', '/api/keys/test', {})[1])
        self.assertEqual(r['result'], 'invalid')
        self.assertIn('did not accept', r['message'])
        code, body = http(self.port, 'POST', '/api/keys', {'client_id': CID, 'client_secret': SECRET, 'redirect_uri': 'http://localhost:3000/callback'})
        self.assertEqual(code, 200)
        for s in (SECRET, CID, CID.split('.')[0]):
            self.assertNotIn(s.encode(), body)
        self.assertTrue(json.loads(body)['keys_saved'])
        r = json.loads(http(self.port, 'POST', '/api/keys/test', {})[1])
        self.assertEqual(r['result'], 'valid', r)
        self.assertIn('recognised', r['message'])
        self.assertEqual(S.status_info()['status'], 'not_configured')
        self.assertEqual(oct(os.stat(S.DATA).st_mode & 0o777), '0o700')
        self.assertEqual(oct(os.stat(S.DATA / 'client.json').st_mode & 0o777), '0o600')
        code, _ = http(self.port, 'POST', '/api/keys', {'client_id': CID, 'client_secret': SECRET, 'redirect_uri': 'http://box.example/callback'})
        self.assertEqual(code, 400)

        # ---- step 5: OAuth (state, PKCE S256, offline access, forced consent)
        start = S.login_start()
        q = {k: v[0] for k, v in parse_qs(urlsplit(start['authorize_url']).query).items()}
        self.assertTrue(start['authorize_url'].startswith('https://accounts.google.com/o/oauth2/v2/auth?'))
        self.assertEqual(q['scope'].split(), SCOPES)
        self.assertEqual((q['access_type'], q['prompt'], q['code_challenge_method'], q['response_type']), ('offline', 'consent', 'S256', 'code'))
        self.assertRegex(q['code_challenge'], r'^[A-Za-z0-9_-]{43}$')
        self.assertTrue(start['paste_back'])
        with self.assertRaises(S.Problem):
            S.login_finish('http://localhost:3000/callback?code=good-code&state=not-the-state')
        with self.assertRaises(S.Problem) as e:
            S.login_finish(f"http://localhost:3000/callback?error=access_denied&state={q['state']}")
        self.assertIn('not to allow', e.exception.message)
        # a wrong PKCE verifier is refused by "Google"
        start = S.login_start()
        q = {k: v[0] for k, v in parse_qs(urlsplit(start['authorize_url']).query).items()}
        FAKE.challenges['other-code'] = 'A' * 43
        with self.assertRaises(S.Problem) as e:
            S.login_finish(f"http://localhost:3000/callback?state={q['state']}&code=other-code")
        self.assertIn('refused', e.exception.message)
        FAKE.force_429, FAKE.force_401 = 1, 1        # first data calls: one rate limit, one expired token
        _, q = self.connect()
        self.wait_sync()
        with self.assertRaises(S.Problem):          # state is single-use
            S.login_finish(f"http://localhost:3000/callback?code=good-code&state={q['state']}")
        st = S.status_info()
        self.assertEqual(st['status'], 'connected', st)
        n_sleep = len(FAKE.data['sleep'])
        self.assertEqual(st['counts'], {'days': DAYS, 'sleeps': n_sleep, 'workouts': len(FAKE.data['exercise'])}, st)
        self.assertGreater(n_sleep, 25)                 # forces sleep paging
        self.assertEqual(st['contract'], 'wizard.health-data/1')
        self.assertEqual(st['source'], {'id': 'wizard-fitbit', 'provider': 'fitbit', 'name': 'Fitbit', 'app_path': '/wizard-fitbit/'})
        self.assertFalse(st['scales']['readiness']['available'])
        self.assertFalse(st['provides']['readiness'])
        self.assertEqual(st['scales']['load']['label'], 'Active Zone Minutes')
        self.assertIsNone(st['scales']['load']['max'])
        self.assertEqual(st['scales']['load']['scale_max'], 60)
        self.assertEqual(st['scales']['load']['targets'], {'high': [22, 45], 'medium': [10, 22], 'low': [0, 10]})
        self.assertIsNone(st['setup']['missing_scopes'])
        ends = dt.datetime.fromisoformat(st['setup']['signin_ends'].replace('Z', '+00:00'))
        self.assertAlmostEqual((ends - dt.datetime.now(dt.timezone.utc)).total_seconds() / 86400, 7, delta=0.01)
        self.assertEqual(oct(os.stat(S.DATA / 'tokens.json').st_mode & 0o777), '0o600')
        sleep_pages = [c for c in FAKE.calls if c[0] == 'GET' and c[1] == 'sleep']
        self.assertTrue(any(c[2].get('pageToken') for c in sleep_pages))
        self.assertTrue(all(int(c[2]['pageSize']) == 25 for c in sleep_pages))
        cal = [c for c in FAKE.calls if c[1] == 'total-calories']
        self.assertGreaterEqual(len(cal), 5)               # 61 days in windows of at most 14
        self.assertTrue(all(c[2]['windowSizeDays'] == 1 for c in cal))

        # ---- mapping onto the contract with the docs' sample payloads
        d0 = (DAY0 + dt.timedelta(days=1)).isoformat()     # i = 0: codelab walk, steps 3822
        r = S.contract_days({'from': d0, 'to': d0})
        self.assertEqual(r['dataset'], st['dataset'])
        d = r['days'][0]
        check_day(self, d)
        self.assertEqual(d['date'], d0)
        self.assertIsNone(d['readiness'], 'never invent a readiness score')
        self.assertEqual(d['hrv_ms'], 41.2)
        self.assertEqual(d['resting_hr_bpm'], 55)
        self.assertEqual(d['spo2_pct'], 96.4)
        self.assertEqual(d['respiratory_rate'], 14.8)
        self.assertEqual((d['skin_temp_c'], d['skin_temp_delta_c']), (33.9, 0.3))
        sl = d['sleep']
        self.assertEqual((sl['asleep_h'], sl['in_bed_h'], sl['awake_h']), (7.58, 8.0, 0.42))
        self.assertEqual((sl['light_h'], sl['deep_h'], sl['rem_h']), (4.17, 1.5, 1.92))
        self.assertEqual(sl['efficiency_pct'], 95)
        self.assertIsNone(sl['need_h'])
        self.assertIsNone(sl['score'])
        self.assertEqual(sl['respiratory_rate'], 14.8)
        self.assertEqual(sl['end'], z(local(DAY0 + dt.timedelta(days=1), 6, 30)).replace('Z', '.000Z'))
        self.assertEqual(len(sl['naps']), 1)
        self.assertEqual(sl['naps'][0]['asleep_h'], 0.58)
        self.assertEqual(d['load'], {'value': 10, 'max': None, 'label': 'Active Zone Minutes', 'scale_max': 60, 'unit': 'min'})
        self.assertEqual(d['extras']['fitbit']['azm'], {'fat_burn': 10, 'cardio': 0, 'peak': 0, 'total': 10})
        self.assertEqual(d['steps'], 3822)
        self.assertEqual(d['calories_kcal'], 2211)
        self.assertFalse(d['extras']['fitbit']['readiness_available'])
        w = d['workouts'][0]
        self.assertEqual((w['sport'], w['minutes'], w['avg_hr_bpm'], w['distance_m'], w['calories_kcal']), ('Walk', 15, 81, 1609, 17))
        self.assertEqual(w['load']['value'], 0)
        self.assertEqual(w['extras']['fitbit']['zones_min'], {'light': 15.0})
        self.assertEqual(w['id'], '8896720705097069096')
        d_cardio = S.contract_days({'from': (DAY0 + dt.timedelta(days=6)).isoformat(), 'to': (DAY0 + dt.timedelta(days=6)).isoformat()})['days'][0]
        self.assertEqual(d_cardio['load']['value'], 2 * 5 + 2 * 1 + 10 + 5)      # i = 5: cardio 10, peak 2, fat burn 15
        off_day = S.contract_days({'from': (DAY0 + dt.timedelta(days=7)).isoformat(), 'to': (DAY0 + dt.timedelta(days=7)).isoformat()})['days'][0]
        self.assertIsNone(off_day['load'], 'a day the watch was not worn has no load, not zero')
        self.assertIsNone(off_day['steps'])
        today = S.contract_days({'from': TODAY.isoformat()})['days']
        self.assertEqual([x['date'] for x in today], [TODAY.isoformat()])
        self.assertIsNone(today[0]['sleep'])
        for x in S.contract_days({'limit': 500})['days']:
            check_day(self, x)
            self.assertIsNone(x['readiness'])
        with self.assertRaises(S.Problem):
            S.contract_days({'from': 'yesterday'})
        with self.assertRaises(S.Problem):
            S.contract_days({'changed_since': 'w1.3'})

        # paging: oldest first, no gaps or repeats, same cursor on every page
        seen, page, cursors = [], None, set()
        while True:
            r = S.contract_days(dict({'limit': 15}, **({'page': page} if page else {})))
            seen += [x['date'] for x in r['days']]
            cursors.add(r['cursor'])
            page = r['next_page']
            if not page:
                break
        self.assertEqual(len(seen), DAYS)
        self.assertEqual(seen, sorted(set(seen)))
        self.assertEqual(len(cursors), 1)
        cursor = cursors.pop()

        # changed_since: overlap re-sync re-emits nothing; an edited sleep, a deleted walk -> exactly those days
        self.assertEqual(S.contract_days({'changed_since': cursor})['days'], [])
        S.sync(full=True)
        self.assertEqual(S.contract_days({'changed_since': cursor})['days'], [], 'an unchanged re-sync must not re-emit days')
        edited = FAKE.data['sleep'][-1]
        edited['sleep']['summary']['minutesAsleep'] = '400'
        gone = FAKE.data['exercise'].pop()
        gone_day = (dt.datetime.fromisoformat(gone['exercise']['interval']['startTime'].replace('Z', '+00:00')) + dt.timedelta(seconds=OFF)).date().isoformat()
        S.sync()
        ch = S.contract_days({'changed_since': cursor})
        dates = [x['date'] for x in ch['days']]
        self.assertEqual(sorted(dates), sorted({gone_day, (TODAY - dt.timedelta(days=1)).isoformat()}), dates)
        by = {x['date']: x for x in ch['days']}
        self.assertEqual(by[(TODAY - dt.timedelta(days=1)).isoformat()]['sleep']['asleep_h'], 6.67)
        self.assertEqual(by[gone_day]['workouts'], [])
        self.assertTrue(by[gone_day]['updated_at'])
        self.assertNotEqual(ch['cursor'], cursor)
        self.assertEqual(S.contract_days({'changed_since': ch['cursor']})['days'], [])
        self.assertEqual(len(S.contract_days({'changed_since': ''})['days']), DAYS)

        # ---- incremental sync asks from the last sync minus the overlap
        FAKE.calls.clear()
        S.sync()
        sl = [c for c in FAKE.calls if c[1] == 'sleep']
        lo = re.search(r'>= "(\d{4}-\d{2}-\d{2})"', sl[0][2]['filter']).group(1)
        self.assertAlmostEqual((TODAY - dt.date.fromisoformat(lo)).days, S.OVERLAP_DAYS + 1, delta=1)
        self.assertEqual(S.status_info()['counts']['days'], DAYS)

        # ---- refresh: Google usually keeps the refresh token; when it does rotate, the new one is stored first
        t = S._tokens(); old_refresh = t['refresh_token']; t['expires_at'] = 0; S._save_tokens(t)
        S.api_call('GET', '/users/me/dataTypes/daily-resting-heart-rate/dataPoints',
                   {'filter': f'daily_resting_heart_rate.date >= "{TODAY}" AND daily_resting_heart_rate.date < "{TODAY + dt.timedelta(days=1)}"'})
        t2 = S._tokens()
        self.assertEqual(t2['refresh_token'], old_refresh)
        self.assertEqual(t2['access_token'], FAKE.access)
        self.assertIn('refresh_expires_at', t2, 'the 7-day Testing-mode end survives a refresh without a new refresh token')
        FAKE.rotate = True
        t2['expires_at'] = 0; S._save_tokens(t2)
        S.api_call('GET', '/users/me/dataTypes/sleep/dataPoints', {'filter': f'sleep.interval.civil_end_time >= "{TODAY}" AND sleep.interval.civil_end_time < "{TODAY + dt.timedelta(days=1)}"', 'pageSize': 25})
        t3 = S._tokens()
        self.assertNotEqual(t3['refresh_token'], old_refresh)
        self.assertEqual(t3['refresh_token'], FAKE.refresh)
        FAKE.rotate = False

        # ---- 429 that does not clear: rate_limited, data still readable, then resumes
        FAKE.force_429 = 50
        with self.assertRaises(S.GoogleError):
            S.sync()
        self.assertEqual(S.status_info()['status'], 'rate_limited')
        self.assertEqual(len(S.contract_days({'limit': 500})['days']), DAYS)
        FAKE.force_429 = 0
        S._update_state(rate_limited_until=None)
        S.sync()
        self.assertEqual(S.status_info()['status'], 'connected')

        # ---- HTTP + MCP
        code, body = http(self.port, 'POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        self.assertEqual([t['name'] for t in json.loads(body)['result']['tools']], ['health_data_status', 'health_data_sync', 'health_data_days'])
        for name, args in [('health_data_status', {}), ('health_data_days', {'from': d0}), ('health_data_days', {'changed_since': ''})]:
            code, body = http(self.port, 'POST', '/mcp', {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}})
            res = json.loads(body)['result']
            self.assertFalse(res['isError'], (name, res))
            for secret in (SECRET, FAKE.access, FAKE.refresh, CID):
                self.assertNotIn(secret, body.decode())
        code, _ = http(self.port, 'POST', '/mcp', {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'}, {'Tailscale-User-Login': 'someone@example.com'})
        self.assertEqual(code, 403)
        self.assertEqual(http(self.port, 'GET', '/api/status', headers={'Host': 'evil.example:18998'})[0], 403)
        self.assertEqual(http(self.port, 'GET', '/api/status', headers={'Host': 'box.example.ts.net:8444'})[0], 200)
        self.assertEqual(http(self.port, 'POST', '/api/sync', {}, {'Host': 'box.example.ts.net:8444', 'Origin': 'https://evil.example'})[0], 403)
        for path in ('/api/status', '/api/setup', '/api/days', '/api/days?changed_since=', '/wizard-fitbit/api/status', '/healthz', '/', '/app.js', '/icon.svg'):
            code, body = http(self.port, 'GET', path)
            self.assertEqual(code, 200, path)
            for secret in (SECRET, FAKE.access, FAKE.refresh):
                self.assertNotIn(secret, body.decode(), path)
        for hdr in ('X-Forwarded-For', 'Tailscale-User-Login', 'Forwarded', 'X-Real-IP', 'CF-Connecting-IP'):
            self.assertEqual(http(self.port, 'GET', '/api/days', headers={hdr: 'x'})[0], 403, hdr)
        self.assertEqual(http(self.port, 'GET', '/api/status', headers={'Host': 'box.example.ts.net:8444', 'X-Forwarded-For': '100.1.2.3'})[0], 200)

        # ---- the app's own callback route (https Redirect URL case)
        start = S.login_start()
        q = {k: v[0] for k, v in parse_qs(urlsplit(start['authorize_url']).query).items()}
        FAKE.challenges['cb-code'] = q['code_challenge']
        code, body = http(self.port, 'GET', f"/wizard-fitbit/callback?state={q['state']}&code=cb-code&scope=x")
        self.assertEqual(code, 200, body)
        self.assertIn(b'Fitbit connected', body)
        self.wait_sync()

        # ---- partial consent: the owner unticked Sleep -> sleep is skipped, named, everything else syncs
        while S.SYNC_LOCK.locked():
            time.sleep(0.05)
        FAKE.granted = [s for s in SCOPES if not s.endswith('sleep.readonly')]
        self.connect('partial-code')
        self.wait_sync()
        while S.SYNC_LOCK.locked():
            time.sleep(0.05)
        st = S.status_info()
        self.assertEqual(st['status'], 'connected')
        self.assertEqual(st['setup']['missing_scopes'], ['Sleep'])
        self.assertIsNone(st['error'])
        # and a 403 for a type Google refuses (scope string claims it) is skipped, not fatal
        S._save_tokens(dict(S._tokens(), scope=' '.join(SCOPES)))
        S.sync()
        self.assertEqual(S.status_info()['setup']['missing_scopes'], ['Sleep'])
        FAKE.granted = list(SCOPES)

        # ---- refresh refused (7-day Testing mode ended) -> needs sign-in, tokens removed, data kept
        FAKE.refresh = 'ended-by-google'
        t = S._tokens(); t['expires_at'] = 0; S._save_tokens(t)
        with self.assertRaises(S.Problem) as e:
            S.api_call('GET', '/users/me/dataTypes/sleep/dataPoints', {'filter': 'x', 'pageSize': 1})
        self.assertIn('7 days', e.exception.message)
        st = S.status_info()
        self.assertEqual(st['status'], 'needs_login')
        self.assertFalse((S.DATA / 'tokens.json').exists())
        self.assertEqual(st['counts']['days'], DAYS)
        ds = st['dataset']

        # ---- disconnect revokes at Google and wipes data; forget wipes keys
        self.connect('last-code')
        self.wait_sync()
        while S.SYNC_LOCK.locked():
            time.sleep(0.05)
        access, refresh = FAKE.access, FAKE.refresh
        code, body = http(self.port, 'POST', '/api/disconnect', {'forget_keys': True})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['status'], 'not_configured')
        self.assertIn(refresh, FAKE.revoked)
        blob = self.files_text()
        for secret in (SECRET, CID, access, refresh):
            self.assertNotIn(secret, blob)
        st = S.status_info()
        self.assertEqual(st['counts'], {'days': 0, 'sleeps': 0, 'workouts': 0})
        self.assertNotEqual(st['dataset'], ds, 'readers must see the copy was wiped')


DAY_KEYS = {'date', 'updated_at', 'readiness', 'hrv_ms', 'resting_hr_bpm', 'spo2_pct', 'skin_temp_c', 'skin_temp_delta_c', 'respiratory_rate',
            'sleep', 'load', 'calories_kcal', 'energy_kj', 'avg_hr_bpm', 'max_hr_bpm', 'steps', 'workouts', 'extras'}
SLEEP_KEYS = {'start', 'end', 'asleep_h', 'in_bed_h', 'awake_h', 'light_h', 'deep_h', 'rem_h', 'score', 'efficiency_pct', 'respiratory_rate', 'need_h', 'naps'}
WORKOUT_KEYS = {'id', 'sport', 'start', 'end', 'minutes', 'load', 'avg_hr_bpm', 'max_hr_bpm', 'calories_kcal', 'energy_kj', 'distance_m', 'zones_min', 'extras'}


def check_day(t, d):
    """The day record shape promised by docs/health-data-contract.md."""
    t.assertTrue(DAY_KEYS <= set(d), DAY_KEYS - set(d))
    t.assertRegex(d['date'], r'^\d{4}-\d{2}-\d{2}$')
    if d['sleep']:
        t.assertTrue(SLEEP_KEYS <= set(d['sleep']), SLEEP_KEYS - set(d['sleep']))
    if d['load']:
        t.assertEqual(set(d['load']), {'value', 'max', 'label', 'scale_max', 'unit'})
        t.assertIsInstance(d['load']['value'], int)
    for w in d['workouts']:
        t.assertTrue(WORKOUT_KEYS <= set(w), WORKOUT_KEYS - set(w))
    t.assertEqual(set(d['extras']), {'fitbit'})


class HealthClassificationTest(unittest.TestCase):
    """What Health does with this source, per the contract doc: recovery 'no score', sleep by hours (no need),
    activity against the AZM targets when a band exists."""

    def classify(self, load, band, scales):
        lo, hi = scales['targets'][band]
        if load < lo:
            return 'room'
        if load <= hi:
            return 'right'
        return 'bit much' if load - hi <= scales['over_margin'] else 'too much'

    def test_targets(self):
        sys.path.insert(0, str(ROOT / 'app'))
        import server
        sc = server.SCALES['load']
        self.assertEqual(self.classify(30, 'high', sc), 'right')
        self.assertEqual(self.classify(12, 'high', sc), 'room')
        self.assertEqual(self.classify(55, 'high', sc), 'bit much')
        self.assertEqual(self.classify(70, 'high', sc), 'too much')
        self.assertEqual(self.classify(5, 'low', sc), 'right')
        self.assertEqual(self.classify(30, 'low', sc), 'too much')
        for band, (lo, hi) in sc['targets'].items():
            self.assertLessEqual(hi, sc['scale_max'], band)
        self.assertIn(sc['daily_goal'], range(sc['targets']['high'][0], sc['targets']['high'][1] + 1))


class IconTest(unittest.TestCase):
    """Own icon: full-colour store square (icon.png from wizard.md) and a silhouette at web/icon.svg the shell tints.
    A generic tracker band with a heart; never Fitbit's or Google's logo or wordmark."""

    def test_icons(self):
        self.assertIn('\nicon: icon.png\n', (ROOT / 'wizard.md').read_text())
        png = (ROOT / 'icon.png').read_bytes()
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual((int.from_bytes(png[16:20], 'big'), int.from_bytes(png[20:24], 'big')), (1024, 1024))
        mask = (ROOT / 'web' / 'icon.svg').read_text()
        self.assertIn('viewBox="0 0 512 512"', mask)
        self.assertNotRegex(mask, r'<text|<image|href=')
        self.assertNotRegex(mask + (ROOT / 'tools' / 'icon.svg').read_text(), r'(?i)fitbit|google')


@unittest.skipUnless(os.environ.get('FITBIT_LIVE_TEST') == '1', 'live Google token endpoint check (set FITBIT_LIVE_TEST=1)')
class LiveTokenEndpointTest(unittest.TestCase):
    """The Test button against Google's REAL token endpoint with made-up credentials: must say 'not accepted'."""

    def test_fake_keys_rejected(self):
        tmp = tempfile.mkdtemp()
        env = dict(os.environ)
        try:
            for k in ('FITBIT_TOKEN_URL',):
                os.environ.pop(k, None)
            os.environ['DATA_DIR'] = tmp + '/private'
            sys.path.insert(0, str(ROOT / 'app'))
            import server
            S = importlib.reload(server)
            self.assertEqual(S.TOKEN_URL, 'https://oauth2.googleapis.com/token')
            S.save_keys({'client_id': '123456789012-' + os.urandom(12).hex() + '.apps.googleusercontent.com',
                         'client_secret': 'GOCSPX-' + os.urandom(14).hex(), 'redirect_uri': 'http://localhost:3000/callback'})
            r = S.test_keys()
            print('LIVE Google token endpoint ->', r)
            self.assertEqual(r['result'], 'invalid')
            self.assertIn('did not accept', r['message'])
        finally:
            os.environ.clear(); os.environ.update(env)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
