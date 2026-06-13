"""Debug Google sign-in: search for API details and try correct format."""
import re, sys, json, urllib.parse, html as html_mod
sys.path.insert(0, r"C:\Users\User\unifiedme-ai\cb_keygen")
from curl_cffi import requests
from cb_keygen import CodeBuddyKeygen

EMAIL = "AlfianAditHakim@owpeeecuy.dev"

kg = CodeBuddyKeygen(email=EMAIL, password="", interactive=False)
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

# Search for CSRF / token / gapi patterns
tokens = re.findall(r'["\']([A-Za-z0-9_\-]{20,})["\']', html)
print("Long tokens found:", len(tokens))
# Look for API request patterns
api_patterns = [
    (r'["\']([^"\']*signin[^"\']*identifier)["\']', "Signin endpoint"),
    (r'SNlM0e["\']\s*:\s*["\']([^"\']+)["\']', "SNlM0e token"),
    (r'AF_initDataCallback[^;]+', "AF init data"),
    (r'f\.req\b', "f.req format"),
]
for pat, name in api_patterns:
    matches = re.findall(pat, html, re.I)
    if matches:
        print(f"\n{name}: {len(matches)} matches")
        for m in matches[:3]:
            s = m if isinstance(m, str) else m[0]
            print(f"  {s[:200]}")

# Extract all form fields from HTML more thoroughly
from bs4 import BeautifulSoup
soup = BeautifulSoup(html, "lxml")
forms = soup.find_all("form")
print(f"\nTotal <form> tags: {len(forms)}")
for i, form in enumerate(forms):
    print(f"\nForm {i}: action={form.get('action', '(none)')[:80]}, method={form.get('method', '(none)')}")
    inputs = form.find_all("input")
    print(f"  Inputs: {len(inputs)}")
    for inp in inputs:
        name = inp.get("name", "(unnamed)")
        value = inp.get("value", "(no value)")
        typ = inp.get("type", "")
        print(f"    [{typ}] {name} = {str(value)[:60]}")

# Look for JavaScript submit handlers
for script in soup.find_all("script"):
    text = script.string or ""
    if "submit" in text.lower() and len(text) < 5000:
        print(f"\nScript with submit: {text[:500]}")

# Try the f.req format (Google's internal API format)
print("\n\n=== Attempt 3: f.req format ===")
# f.req is a JSON array format
freq = json.dumps([EMAIL])
resp3 = session.post(
    resp.url,
    data=f"f.req={urllib.parse.quote(freq)}",
    headers={
        **headers,
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        "Origin": "https://accounts.google.com",
        "Referer": resp.url,
    },
    allow_redirects=False,
    timeout=30,
)
loc3 = resp3.headers.get("Location", "")
print(f"HTTP {resp3.status_code}, Location: {loc3[:150] if loc3 else '(none)'}")

# Try with proper multipart/form-data format
print("\n=== Attempt 4: Direct form field submission to v3 endpoint ===")
v3_url = "https://accounts.google.com/v3/signin/identifier"
parsed = urllib.parse.urlparse(resp.url)
params = urllib.parse.parse_qs(parsed.query)

# Extract hidden fields
hidden = {}
for inp in soup.find_all("input", type="hidden"):
    name = inp.get("name")
    value = inp.get("value", "")
    if name:
        hidden[name] = value

# Core form fields expected by Google
vd = {
    "identifier": EMAIL,
    "continue": params.get("continue", [""])[0],
    "service": params.get("service", [""])[0],
    "flowName": params.get("flowName", [""])[0],
    "dsh": params.get("dsh", [""])[0],
    "hl": "en",
}
# Merge hidden fields
vd.update(hidden)
vd = {k: v for k, v in vd.items() if v}

print(f"POST to {v3_url}")
print(f"Fields: {list(vd.keys())}")

resp4 = session.post(
    v3_url,
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
loc4 = resp4.headers.get("Location", "")
print(f"HTTP {resp4.status_code}, Location: {loc4[:200] if loc4 else '(none)'}")
ct4 = resp4.headers.get("Content-Type", "")
if loc4:
    print(f"Content-Type: {ct4}")
else:
    print(f"Content-Type: {ct4}")
    sz = len(resp4.text)
    title = re.search(r'<title[^>]*>(.*?)</title>', resp4.text, re.I|re.S)
    print(f"Size: {sz}, Title: {title.group(1)[:80] if title else '(none)'}")
