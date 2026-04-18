import requests, re

r = requests.get('https://js.stripe.com/v3/', timeout=15,
    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0'})

text = r.text
print(f"JS size: {len(text)} bytes")

# Look for payment_user_agent string
m = re.search(r'payment_user_agent[^"]*"([^"]+)"', text)
if m:
    print(f"payment_user_agent: {m.group(1)}")

# Look for stripe.js hash pattern (10 hex chars)
hashes = re.findall(r'stripe\.js/([a-f0-9]{10})', text)
if hashes:
    print(f"stripe.js hashes found: {hashes[:5]}")

# Look for version string
vers = re.findall(r'"version"\s*:\s*"([a-f0-9]{8,12})"', text)
if vers:
    print(f"version strings: {vers[:5]}")

# ETag as fallback fingerprint
print(f"ETag: {r.headers.get('ETag','none')}")

# Look for 10-char hex that appears near 'checkout'
chunk = text[text.find('checkout') - 200 : text.find('checkout') + 200] if 'checkout' in text else ''
hexes = re.findall(r'[a-f0-9]{10}', chunk)
print(f"Hex near 'checkout': {hexes[:5]}")
