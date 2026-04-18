"""
get_paypal_token.py
====================
PayPal se naya access token generate karo.
Usage:
    python get_paypal_token.py CLIENT_ID CLIENT_SECRET
"""
import sys
import requests
import base64

def get_token(client_id, client_secret):
    url = "https://api-m.paypal.com/v1/oauth2/token"
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"grant_type": "client_credentials"}
    
    print("[*] PayPal se token le raha hoon...")
    r = requests.post(url, headers=headers, data=data)
    
    if r.status_code == 200:
        token = r.json().get("access_token", "")
        expires = r.json().get("expires_in", 0)
        print(f"\n[OK] Token mila! ({expires//3600}h {(expires%3600)//60}m valid)\n")
        print("=" * 60)
        print(f"PAYPAL_AU_TOKEN={token}")
        print("=" * 60)
        print("\n[*] Upar wali value .env file mein paste karo aur deploy karo.")
    else:
        print(f"[ERROR] {r.status_code}: {r.text}")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python get_paypal_token.py CLIENT_ID CLIENT_SECRET")
        print("\nPayPal Developer Console se credentials lo:")
        print("  https://developer.paypal.com -> My Apps & Credentials -> Live")
        sys.exit(1)
    get_token(sys.argv[1], sys.argv[2])
