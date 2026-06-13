"""Find Google sign-in API request format from page JS."""
import re, sys, json, urllib.parse
sys.path.insert(0, r"C:\Users\User\unifiedme-ai\cb_keygen")
from curl_cffi import requests
from cb_keygen import CodeBuddyKeygen

EMAIL = "AlfianAditHakim@owpeeecuy.dev"
PASSWORD = "qwertyui"

kg = CodeBuddyKeygen(email=EMAIL, password=PASSWORD, interactive=False)
state, auth_url = kg.bootstrap()
kc_url = kg._construct_keycloak_url()

session = requests.Session()
session.impersonate = "chrome120"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
resp = session.get(kc_url, headers=headers, allow_redirects=True, timeout=30)
html = resp.text

# Find the SNlM0e token  
snlm0e = re.search(r'SNlM0e["\']\s*:\s*["\']([^"\']+)["\']', html)
print(f"SNlM0e: {snlm0e.group(1) if snlm0e else 'NOT FOUND'}")

# Search for API endpoint patterns in JS scripts
from bs4 import BeautifulSoup
soup = BeautifulSoup(html, "lxml")
scripts = soup.find_all("script")

# Look for the sign-in API call patterns
for script in scripts:
    text = script.string or ""
    if len(text) < 100 or len(text) > 200000:
        continue
    
    # Look for identifier-related API patterns
    for pattern in [
        r'fetch\([^)]+signin[^)]+\)',
        r'XMLHttpRequest[^;]+signin[^;]+',
        r'signin[^;]*identifier[^;]+',
        r'f\.req',
        r'gapi\.load',
        r'GLIF',
        r'\/_\/signin',
        r'F\.cC',
    ]:
        matches = re.findall(pattern, text, re.I)
        if matches:
            print(f"\nPattern '{pattern}' found: {len(matches)}")
            for m in matches[:3]:
                print(f"  {m[:200]}")

# Specifically look at the first few scripts which may contain the bootstrapping code
print("\n=== Early scripts check ===")
for i, script in enumerate(soup.find_all("script")[:10]):
    text = script.string or ""
    if not text.strip():
        continue
    # Get the src attribute
    src = script.get("src", "")
    print(f"\nScript {i}: src={src[:80] if src else '(inline)'}, len={len(text)}")
    if "signin" in text.lower() and len(text) < 5000:
        idx = text.lower().find("signin")
        print(f"  Context: {text[max(0,idx-50):idx+150][:200]}")

# Also look at second-level scripts loaded from the page (they're in the HTML body)
print("\n=== Looking for SL/GLIF API calls ===")
for script in scripts:
    text = script.string or ""
    if len(text) > 1000:
        api_calls = re.findall(r'https?://accounts\.google\.com[^"\'\s)]+', text)
        if api_calls:
            print(f"\nAPI calls in {len(text)} byte script:")
            for url in api_calls[:5]:
                print(f"  {url[:150]}")
