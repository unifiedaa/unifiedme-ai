"""Debug Google sign-in endpoint and try submitting real credentials."""
import re, sys, json, urllib.parse
sys.path.insert(0, r"C:\Users\User\unifiedme-ai\cb_keygen")
from curl_cffi import requests
from cb_keygen import CodeBuddyKeygen

kg = CodeBuddyKeygen(email="", password="", interactive=False)
state, auth_url = kg.bootstrap()
kc_url = kg._construct_keycloak_url()

session = requests.Session()
session.impersonate = "chrome120"
resp = session.get(
    kc_url,
    headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    },
    allow_redirects=True,
    timeout=30,
)
html = resp.text
print(f"Final URL: {resp.url[:150]}...")
print(f"HTML size: {len(html)} bytes")

# Search for ANY form-related patterns
patterns = [
    (r'/_/signin/sl/v\d+/[a-z]+', "Google sign-in API"),
    (r'/signin/[a-z/]+', "Signin path"),
    (r'action\s*=\s*["\']([^"\']+)["\']', "Form action"),
    (r'fetch\s*\(["\'][^"\']*signin[^"\']*["\']', "Fetch signin"),
    (r'f\.req', "f.req format"),
    (r'FIRST_SIGNIN|first.signin|identifier', "Identifier pattern"),
]

for pat, name in patterns:
    matches = re.findall(pat, html, re.I)
    if matches:
        print(f"\n{name}:")
        for m in matches[:5]:
            s = m if isinstance(m, str) else m[0]
            print(f"  {s[:120]}")

# Also look at script tags for sign-in related code
from bs4 import BeautifulSoup
soup = BeautifulSoup(html, "lxml")
scripts = soup.find_all("script")
for i, script in enumerate(scripts):
    text = script.string or ""
    if "signin" in text.lower() or "identifier" in text.lower():
        # Extract the relevant part
        idx = text.lower().find("signin" if "signin" in text.lower() else "identifier")
        if idx >= 0:
            snippet = text[max(0,idx-100):idx+200]
            print(f"\nScript {i}: ...{snippet}...")
            if len(text) < 50000:
                print(f"  FULL: {text[:500]}")

# Try the Google sign-in API endpoint directly
print("\n\n=== Trying sign-in API direct ===")
# Look for the endpoint in page data
api_endpoints = re.findall(r'["\'](/[^"\']*signin[^"\']*identifier)["\']', html)
print(f"Found {len(api_endpoints)} API endpoint patterns")
for ep in api_endpoints[:5]:
    print(f"  {ep}")

# Try the most common endpoint
test_url = "https://accounts.google.com/_/signin/sl/v1/identifier"
# Extract params from current URL
parsed = urllib.parse.urlparse(resp.url)
params = urllib.parse.parse_qs(parsed.query)
# The key params to pass through
key_params = {}
for k in ["continue", "service", "hl", "flowName", "dsh", "authuser"]:
    if k in params:
        key_params[k] = params[k][0]

print(f"\nKey params from URL:")
for k, v in key_params.items():
    print(f"  {k} = {v[:80]}")

# Build the endpoint URL with params
if key_params:
    qs = urllib.parse.urlencode(key_params)
    full_test_url = f"{test_url}?{qs}"
    print(f"\nTest URL: {full_test_url[:150]}...")
    
    # Also extract hidden input values from the page
    from cb_keygen import extract_google_form_fields
    hidden = extract_google_form_fields(html)
    print(f"Hidden fields: {json.dumps(hidden, indent=2)[:200]}")
    
    # Prepare POST data
    post_data = {**hidden, "identifier": "test@example.com"}
    # Also add URL params that might be needed
    for k, v in key_params.items():
        if k not in post_data:
            post_data[k] = v
    
    print(f"\nPOST data keys: {list(post_data.keys())}")
    
    resp2 = session.post(
        full_test_url,
        data=post_data,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://accounts.google.com",
            "Referer": resp.url,
        },
        allow_redirects=False,
        timeout=30,
    )
    print(f"\nDirect API call → HTTP {resp2.status_code}")
    print(f"Location: {resp2.headers.get('Location', '(none)')[:120]}")
    print(f"Content-Type: {resp2.headers.get('Content-Type', '(none)')}")
    
    if resp2.status_code == 200:
        ct = resp2.headers.get("Content-Type", "")
        if "json" in ct.lower() or resp2.text.startswith("["):
            print(f"JSON response: {resp2.text[:500]}")
        else:
            # Check if it's a redirect page (HTML)
            if "<form" in resp2.text.lower():
                form_action = re.search(r'<form[^>]*action=["\']([^"\']+)["\']', resp2.text, re.I)
                if form_action:
                    print(f"Form action in response: {form_action.group(1)[:120]}")
            title = re.search(r'<title[^>]*>(.*?)</title>', resp2.text, re.I | re.S)
            print(f"Title: {title.group(1)[:80] if title else '(none)'}")
            print(f"Response size: {len(resp2.text)} bytes")
