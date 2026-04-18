import os
import time
import requests as _req

def stripe_check(checkout_url, cc, mes, ano, cvc):
    """
    Standalone Stripe checkout check — does NOT import file1 to avoid circular imports.
    Calls the Stripe checkout page directly via HTTP.
    Returns a dict: {status, message, merchant, amount, time}
    """
    ccx = f"{cc}|{mes}|20{ano}|{cvc}" if len(ano) == 2 else f"{cc}|{mes}|{ano}|{cvc}"
    t0 = time.time()
    try:
        session = _req.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        r1 = session.get(checkout_url, headers=headers, timeout=20, allow_redirects=True)
        elapsed = round(time.time() - t0, 2)

        if r1.status_code != 200:
            return {"status": "error", "message": f"HTTP {r1.status_code}",
                    "merchant": "N/A", "amount": "N/A", "time": elapsed}

        return {"status": "error", "message": "Direct check not supported — use /xco via bot",
                "merchant": "N/A", "amount": "N/A", "time": elapsed}

    except Exception as e:
        elapsed = round(time.time() - t0, 2)
        return {"status": "error", "message": str(e)[:80],
                "merchant": "N/A", "amount": "N/A", "time": elapsed}
