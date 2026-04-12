# -*- coding: utf-8 -*-
"""
services/ig_reporter.py
Instagram mass reporter — adapted for Telegram bot (.ig command).
Original script by @Antyrx
"""

import requests
import time
import random
import threading
from concurrent.futures import ThreadPoolExecutor

try:
    from fake_useragent import UserAgent as _FUA
    _ua_gen = _FUA()
    def _ua(): return _ua_gen.random
except Exception:
    def _ua(): return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

# ── Report reason map ──────────────────────────────────────────────────────
REASON_MAP = {
    1: ("Spam / Fraud",            "spam"),
    2: ("Don't like it",           "i_just_dont_like_it"),
    3: ("Harassment",              "harassment"),
    4: ("Self-harm",               "self_injury"),
    5: ("Hate speech / Violence",  "hate_speech"),
    6: ("Illegal products",        "sale_of_regulated_goods"),
    7: ("Nudity / Sexual content", "nudity"),
    8: ("Scam / Fraud",            "scam"),
    9: ("Misinformation",          "false_information"),
}

# ── Hardcoded base cookies (working set) ──────────────────────────────────
_BASE_COOKIES = {
    'datr':     'OqhFaSvz3896RM699JeDRERS',
    'ig_did':   '24E8448B-DA5C-4392-A68C-6F71FA8DD8FE',
    'mid':      'aUWoPwAEAAHn-v0cDfeyq7L3OXrg',
    'ig_nrcb':  '1',
    'ps_l':     '1',
    'ps_n':     '1',
    'csrftoken': 'a23WoaP6T9yoOfR5b2UpEZkMNTDSx6Qa',
    'dpr':      '3.0234789848327637',
    'wd':       '891x1671',
}

_BASE_HEADERS = {
    'authority':                    'www.instagram.com',
    'accept':                       '*/*',
    'accept-language':              'en-US,en;q=0.9',
    'content-type':                 'application/x-www-form-urlencoded',
    'origin':                       'https://www.instagram.com',
    'referer':                      'https://www.instagram.com/',
    'sec-ch-prefers-color-scheme':  'dark',
    'sec-ch-ua':                    '"Chromium";v="137", "Not/A)Brand";v="24"',
    'sec-ch-ua-mobile':             '?0',
    'sec-ch-ua-platform':           '"Linux"',
    'sec-fetch-dest':               'empty',
    'sec-fetch-mode':               'cors',
    'sec-fetch-site':               'same-origin',
    'user-agent':                   _ua(),
    'x-asbd-id':                    '359341',
    'x-csrftoken':                  'a23WoaP6T9yoOfR5b2UpEZkMNTDSx6Qa',
    'x-ig-app-id':                  '936619743392459',
    'x-ig-www-claim':               '0',
    'x-instagram-ajax':             '1031623017',
    'x-requested-with':             'XMLHttpRequest',
    'x-web-session-id':             'xm1wtf:t1yo2p:xxabri',
}


# ─────────────────────────────────────────────────────────────────────────────

def ig_login(username: str, password: str) -> dict:
    """
    Log into Instagram. Returns dict:
      { 'ok': bool, 'session': requests.Session, 'csrf': str,
        'user_id': str, 'error': str }
    """
    session = requests.Session()
    cookies = dict(_BASE_COOKIES)
    headers = dict(_BASE_HEADERS)
    headers['user-agent'] = _ua()

    timestamp = str(int(time.time()))
    login_data = {
        'enc_password':              f'#PWD_INSTAGRAM_BROWSER:0:{timestamp}:{password}',
        'caaF2DebugGroup':           '0',
        'isPrivacyPortalReq':        'false',
        'loginAttemptSubmissionCount': '0',
        'optIntoOneTap':             'false',
        'queryParams':               '{}',
        'trustedDeviceRecords':      '{}',
        'username':                  username,
        'jazoest':                   '22669',
    }
    try:
        resp = session.post(
            'https://www.instagram.com/api/v1/web/accounts/login/ajax/',
            cookies=cookies, headers=headers, data=login_data, timeout=15
        )
        txt = resp.text
        if '"user":true' not in txt or '"authenticated":true' not in txt:
            if '"checkpoint_required"' in txt:
                return {'ok': False, 'error': 'Checkpoint required (2FA/security check)'}
            if '"bad_password"' in txt:
                return {'ok': False, 'error': 'Wrong password'}
            if '"invalid_user"' in txt:
                return {'ok': False, 'error': 'Invalid Instagram username'}
            return {'ok': False, 'error': 'Login failed'}

        csrf  = session.cookies.get('csrftoken', cookies['csrftoken'])
        uid   = session.cookies.get('ds_user_id', '')
        return {'ok': True, 'session': session, 'csrf': csrf, 'user_id': uid, 'error': None}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def ig_login_from_cookies(sessionid: str, csrftoken: str) -> dict:
    """
    Bypass password login — use browser session cookies directly.
    Get them from: Instagram.com → F12 → Application → Cookies
      • sessionid
      • csrftoken
    Returns same dict shape as ig_login().
    """
    session = requests.Session()
    cookies = dict(_BASE_COOKIES)
    cookies['sessionid'] = sessionid
    cookies['csrftoken'] = csrftoken

    headers = dict(_BASE_HEADERS)
    headers['user-agent']  = _ua()
    headers['x-csrftoken'] = csrftoken

    # Verify session is alive by hitting profile endpoint
    try:
        r = session.get(
            'https://www.instagram.com/api/v1/accounts/current_user/?edit=true',
            cookies=cookies, headers=headers, timeout=12
        )
        data = r.json()
        user = data.get('user', {})
        if not user:
            return {'ok': False, 'error': 'Invalid / expired session cookies'}
        uid = str(user.get('pk', ''))
        # Inject cookies into session
        session.cookies.set('sessionid', sessionid)
        session.cookies.set('csrftoken', csrftoken)
        return {'ok': True, 'session': session, 'csrf': csrftoken, 'user_id': uid, 'error': None}
    except Exception as e:
        return {'ok': False, 'error': f'Cookie auth failed: {e}'}


def get_target_id(session: requests.Session, target_username: str, csrf: str) -> dict:
    """Resolve target username to Instagram user ID."""
    headers = {
        'authority':     'www.instagram.com',
        'accept':        '*/*',
        'accept-language': 'en-US,en;q=0.9',
        'referer':       f'https://www.instagram.com/{target_username}/',
        'user-agent':    _ua(),
        'x-asbd-id':     '359341',
        'x-csrftoken':   csrf,
        'x-ig-app-id':   '936619743392459',
        'x-ig-www-claim': '0',
        'x-requested-with': 'XMLHttpRequest',
    }
    try:
        resp = session.get(
            'https://www.instagram.com/api/v1/users/web_profile_info/',
            params={'username': target_username},
            headers=headers, timeout=12
        )
        data = resp.json()
        uid = data['data']['user']['id']
        return {'ok': True, 'id': uid, 'error': None}
    except Exception as e:
        return {'ok': False, 'id': None, 'error': f'Target not found: {e}'}


def _send_one_report(session, csrf, target_id, reason_tag) -> bool:
    """Send a single report pair. Returns True on confirmed success."""
    cookies = dict(_BASE_COOKIES)
    cookies['csrftoken'] = csrf
    if session.cookies.get('sessionid'):
        cookies['sessionid'] = session.cookies.get('sessionid')
    if session.cookies.get('ds_user_id'):
        cookies['ds_user_id'] = session.cookies.get('ds_user_id')

    headers = dict(_BASE_HEADERS)
    headers['user-agent']  = _ua()
    headers['x-csrftoken'] = csrf

    base_payload = {
        'container_module':       'profilePage',
        'entry_point':            '1',
        'location':               '2',
        'object_id':              target_id,
        'object_type':            '5',
        'frx_prompt_request_type': '2',
        'jazoest':                '22669',
    }

    try:
        p1 = dict(base_payload)
        p1['context'] = '{"tags":["ig_report_account"]}'
        p1['selected_tag_types'] = '["ig_its_inappropriate"]'
        r1 = session.post(
            'https://www.instagram.com/api/v1/web/reports/get_frx_prompt/',
            cookies=cookies, headers=headers, data=p1, timeout=10
        )
        if r1.status_code != 200:
            return False

        p2 = dict(base_payload)
        p2['context'] = f'{{"tags":["ig_report_account","ig_its_inappropriate","{reason_tag}"]}}'
        p2['selected_tag_types'] = f'["{reason_tag}"]'
        r2 = session.post(
            'https://www.instagram.com/api/v1/web/reports/get_frx_prompt/',
            cookies=cookies, headers=headers, data=p2, timeout=10
        )
        txt = r2.text.lower()
        return '"text":"done"' in txt or '"text":"tamam"' in txt or '"text":"تم"' in txt
    except Exception:
        return False


def run_reports(session, csrf, target_id, reason_tag, total,
                threads=10, progress_cb=None, stop_event=None):
    """
    Send `total` reports using `threads` concurrent workers.
    progress_cb(success, fail, done, total) called every ~5% of progress.
    Returns dict: { success, fail, elapsed }
    """
    results = {'success': 0, 'fail': 0}
    lock     = threading.Lock()
    _stop    = stop_event or threading.Event()
    update_every = max(1, total // 20)   # every 5%
    _last_cb  = [0]

    def _worker():
        while not _stop.is_set():
            if results['success'] + results['fail'] >= total:
                break
            ok = _send_one_report(session, csrf, target_id, reason_tag)
            with lock:
                if ok:
                    results['success'] += 1
                else:
                    results['fail'] += 1
                done = results['success'] + results['fail']
                if progress_cb and done - _last_cb[0] >= update_every:
                    _last_cb[0] = done
                    try:
                        progress_cb(results['success'], results['fail'], done, total)
                    except Exception:
                        pass
            time.sleep(random.uniform(0.2, 0.6))

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=threads) as ex:
        futs = [ex.submit(_worker) for _ in range(threads)]
        for f in futs:
            try: f.result()
            except Exception: pass
    elapsed = time.time() - t0

    # Final callback
    if progress_cb:
        done = results['success'] + results['fail']
        try:
            progress_cb(results['success'], results['fail'], done, total)
        except Exception:
            pass

    return {'success': results['success'], 'fail': results['fail'], 'elapsed': elapsed}
