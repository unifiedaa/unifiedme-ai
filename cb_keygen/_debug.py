"""Debug Google sign-in API endpoint."""
import re, sys, json, urllib.parse
sys.path.insert(0, r"C:\Users\User\unifiedme-ai\cb_keygen")
from curl_cffi import requests
from cb_keygen import CodeBuddyKeygen

# Real credentials
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

# Get hidden fields from page
from cb_keygen import extract_google_form_fields

hidden = extract_google_form_fields(html)
parsed = urllib.parse.urlparse(resp.url)
params = urllib.parse.parse_qs(parsed.query)

# Print hidden fields
print("Hidden fields:", json.dumps(hidden, indent=2))
print("URL params:")
for k, v in params.items():
    if v:
        print(f"  {k} = {v[0][:100]}")

# Extract the continue URL from WIZ_global_data
m = re.search(r'"HAZvpc":"([^"]+)"', html)
if m:
    interstitial_url = m.group(1).replace('\\u003d', '=').replace('\\u0026', '&')
    print("\nInterstitial forward URL:", interstitial_url[:200])

# Also try to find the signin URL from init data
m2 = re.search(r'"signInUrl","([^"]+)"', html)
if m2:
    signin_url = m2.group(1).replace('\\u003d', '=').replace('\\u0026', '&')
    print("signInUrl:", signin_url[:200])

# Try post to current URL with proper fields
print("\n=== Attempt 1: POST email to current URL ===")
post_data = {
    "identifier": EMAIL,
    "continue": params.get("continue", [""])[0],
    "flowName": "GeneralOAuthFlow",
    "dsh": params.get("dsh", [""])[0],
    "service": "lso",
    "hl": "en",
}
# Add hidden fields
post_data.update(hidden)
# Remove empty values
post_data = {k: v for k, v in post_data.items() if v}

print("POST keys:", list(post_data.keys()))

resp2 = session.post(
    resp.url,
    data=post_data,
    headers={
        **headers,
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://accounts.google.com",
        "Referer": resp.url,
    },
    allow_redirects=False,
    timeout=30,
)
location = resp2.headers.get("Location", "")
print(f"HTTP {resp2.status_code}, Location: {location[:150] if location else '(none)'}")
if resp2.status_code == 200 and not location:
    title = re.search(r'<title[^>]*>(.*?)</title>', resp2.text, re.I|re.S)
    print(f"Title: {title.group(1)[:80] if title else '(none, size='+str(len(resp2.text))+')'}")
    # Check if password page
    if "password" in resp2.text.lower()[:10000]:
        print("--> PASSWORD PAGE DETECTED!")

# Try API endpoint
print("\n=== Attempt 2: Google API endpoint ===")
api_url = "https://accounts.google.com/_/signin/sl/v1/identifier"
qs = urllib.parse.urlencode({
    "continue": params.get("continue", [""])[0],
    "service": "lso",
    "hl": "en",
    "flowName": "GeneralOAuthFlow",
    "dsh": params.get("dsh", [""])[0],
    "opparams": params.get("opparams", [""])[0] if "opparams" in params else "",
})
full_api_url = f"{api_url}?{qs}"
print(f"API URL: {full_api_url[:200]}")

resp3 = session.post(
    full_api_url,
    data={**post_data},
    headers={
        **headers,
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://accounts.google.com",
        "Referer": resp.url,
        "X-Requested-With": "XMLHttpRequest",
        "X-Same-Domain": "1",
    },
    allow_redirects=False,
    timeout=30,
)
location3 = resp3.headers.get("Location", "")
print(f"HTTP {resp3.status_code}, Location: {location3[:150] if location3 else '(none)'}")
ct = resp3.headers.get("Content-Type", "")
print(f"Content-Type: {ct}")
if resp3.status_code == 200:
    body_preview = resp3.text[:500]
    print(f"Body preview: {body_preview[:300]}")
    if "password" in resp3.text.lower()[:5000]:
        print("--> PASSWORD PAGE DETECTED!")
