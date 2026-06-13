"""Debug Google sign-in with SNlM0e token."""
import re, sys, json, urllib.parse
sys.path.insert(0, r"C:\Users\User\unifiedme-ai\cb_keygen")
from curl_cffi import requests
from bs4 import BeautifulSoup
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
soup = BeautifulSoup(html, "lxml")

# Extract SNlM0e token
snlm0e = re.search(r'["\']SNlM0e["\']\s*:\s*["\']([^"\']+)["\']', html)
print(f"SNlM0e token: {snlm0e.group(1) if snlm0e else 'NOT FOUND'}")

# Extract ALL hidden inputs
hidden = {}
for inp in soup.find_all("input", type="hidden"):
    name = inp.get("name")
    value = inp.get("value", "")
    if name and value:
        hidden[name] = value

# Also look for input with name='gapi' or similar
for inp in soup.find_all("input"):
    name = inp.get("name")
    if name and name not in hidden:
        value = inp.get("value", "")
        if value:
            hidden[name] = value

print(f"\nHidden fields ({len(hidden)}):")
for k, v in hidden.items():
    print(f"  {k} = {str(v)[:80]}")

# URL params
parsed = urllib.parse.urlparse(resp.url)
url_params = urllib.parse.parse_qs(parsed.query)

# Google's form fields for email submission
# The field name for email can be 'identifier', 'Email', 'email', or 'username'
# Also the SNlM0e token should be sent as 'gapi' or similar

# Attempt 1: POST with gapi = SNlM0e token 
vd = {
    "identifier": EMAIL,
    "continue": url_params.get("continue", [""])[0],
    "service": url_params.get("service", [""])[0],
    "flowName": url_params.get("flowName", [""])[0],
    "dsh": url_params.get("dsh", [""])[0],
    "hl": "en",
}
# Add hidden fields
vd.update(hidden)
if snlm0e:
    # Try different field names for the token
    pass

print(f"\n=== Attempt 1: Direct POST to {resp.url.split('?')[0][:60]} ===")
print(f"Fields ({len(vd)}): {list(vd.keys())}")
resp1 = session.post(
    resp.url,
    data=vd,
    headers={
        **headers,
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://accounts.google.com",
        "Referer": resp.url,
    },
    allow_redirects=False,
    timeout=30,
)
loc1 = resp1.headers.get("Location", "")
print(f"HTTP {resp1.status_code}, Location: {loc1[:200] if loc1 else '(none)'}")
if resp1.status_code == 200:
    title = re.search(r'<title[^>]*>(.*?)</title>', resp1.text, re.I|re.S)
    print(f"Title: {title.group(1) if title else 'size='+str(len(resp1.text))}")
    if "password" in resp1.text[:5000].lower():
        print("PASSWORD PAGE!")

# Attempt 2: Just the essential fields
print(f"\n=== Attempt 2: Minimal fields ===")
vd2 = {
    "identifier": EMAIL,
    "continue": url_params.get("continue", [""])[0],
    "dsh": url_params.get("dsh", [""])[0],
}
resp2 = session.post(
    resp.url,
    data=vd2,
    headers={
        **headers,
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://accounts.google.com",
        "Referer": resp.url,
    },
    allow_redirects=False,
    timeout=30,
)
loc2 = resp2.headers.get("Location", "")
print(f"HTTP {resp2.status_code}, Location: {loc2[:200] if loc2 else '(none)'}")
