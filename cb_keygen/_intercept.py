import sys, json, time, base64, os
sys.path.insert(0, r"C:\Users\User\unifiedme-ai\cb_keygen")
from cb_keygen import CodeBuddyKeygen
from camoufox.sync_api import Camoufox
from twocaptcha import TwoCaptcha

EMAIL = "AhmadZidanKurniawan@owpeeecuy.dev"
PASSWORD = "qwertyui"
OUTPUT_FILE = "intercept_log.json"
CAPTCHA_API_KEY = "5ff220e19373d967a494cf020fe454b7"

solver = TwoCaptcha(CAPTCHA_API_KEY)

kg = CodeBuddyKeygen(email=EMAIL, password=PASSWORD, interactive=False)
state, auth_url = kg.bootstrap()
kc_url = kg._construct_keycloak_url()
print(f"state={state[:30]}...")

with Camoufox(headless=False, geoip=False) as browser:
    ctx = browser.new_context()
    ctx.on("page", lambda p: p.on("pageerror", lambda _: None))
    page = ctx.new_page()
    page.on("pageerror", lambda _: None)

    print("[1] Navigating to Keycloak...")
    page.goto(kc_url, wait_until="load", timeout=60000)
    print(f"    {page.url[:80]}")

    import random

    def human_type(locator, text):
        locator.click()
        for char in text:
            locator.press_sequentially(char, delay=random.randint(50, 150))
            if random.random() < 0.1:
                time.sleep(random.uniform(0.1, 0.3))

    def solve_captcha_if_present():
        for attempt in range(5):
            time.sleep(1)
            if "identifier" not in page.url:
                return True
            captcha_input = page.locator("input[aria-label*='Type the text'], input[aria-label*='type the text']")
            try:
                captcha_input.first.wait_for(state="visible", timeout=5000)
            except Exception:
                if "identifier" not in page.url:
                    return True
                return False

            print(f"[captcha] Attempt {attempt+1}, solving...")

            # Re-fill email in case it was cleared after failed captcha
            email_input = page.locator("input[type='email'], input[name='identifier']").first
            try:
                current_email = email_input.input_value()
                if not current_email.strip():
                    print(f"         Re-filling email (was cleared)")
                    email_input.fill("")
                    human_type(email_input, EMAIL)
                    time.sleep(0.5)
            except Exception:
                pass

            captcha_img = page.locator("img").filter(has_not_text="Google")
            b64 = None
            for i in range(captcha_img.count()):
                img = captcha_img.nth(i)
                if not img.is_visible():
                    continue
                box = img.bounding_box()
                if box and box["width"] > 50 and box["height"] > 20 and box["width"] < 400:
                    print(f"         Image: {box['width']:.0f}x{box['height']:.0f}")
                    b64 = base64.b64encode(img.screenshot()).decode()
                    break
            if not b64:
                b64 = base64.b64encode(page.screenshot()).decode()

            result = solver.normal(b64, caseSensitive=1)
            print(f"         Solved: {result['code']}")
            captcha_input.first.fill("")
            human_type(captcha_input.first, result["code"])
            time.sleep(0.3)
            page.locator("#identifierNext, button:has-text('Next')").first.click()
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            time.sleep(3)
            print(f"         -> {page.url[:80]}")
        return "identifier" not in page.url

    # Google sign-in
    if "accounts.google.com" in page.url:
        print("[2] Email...")
        page.wait_for_selector("input[type='email'], input[name='identifier']", timeout=15000)
        human_type(page.locator("input[type='email'], input[name='identifier']").first, EMAIL)

        time.sleep(1)
        if not solve_captcha_if_present():
            page.locator("#identifierNext, button:has-text('Next')").first.click()
            page.wait_for_load_state("networkidle", timeout=15000)
            print(f"    {page.url[:80]}")
            time.sleep(1)
            solve_captcha_if_present()

        # Wait for page to move past identifier
        for _ in range(5):
            if "identifier" not in page.url:
                break
            time.sleep(2)

        # Password
        print("[3] Password...")
        page.wait_for_selector("input[type='password']:visible", timeout=15000)
        human_type(page.locator("input[type='password']:visible").first, PASSWORD)
        time.sleep(0.5)
        page.locator("#passwordNext, button:has-text('Next')").first.click()
        time.sleep(2)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except:
            pass
        print(f"    {page.url[:80]}")

    # Handle consent/welcome/speedbump pages
    prev_url = ""
    for attempt in range(15):
        try:
            url = page.url
        except:
            break
        if "codebuddy.ai" in url and "accounts.google" not in url:
            break
        if url == prev_url and attempt > 3:
            break
        prev_url = url

        clicked = False
        for sel in [
            "text='I understand'",
            "input[value='I understand']",
            "button:has-text('I understand')",
            "[role='button']:has-text('I understand')",
            "text='Continue'",
            "button:has-text('Continue')",
            "text='Accept'",
            "button:has-text('Accept')",
            "text='Allow'",
            "button:has-text('Allow')",
        ]:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(force=True, timeout=5000)
                    clicked = True
                    print(f"[4] Clicked: {sel}")
                    time.sleep(2)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except:
                        pass
                    break
            except:
                continue
        if not clicked:
            time.sleep(2)

    print(f"\n[5] On CodeBuddy: {page.url[:120]}")

    # Navigate to console first so fetch() runs on the correct origin with cookies
    print("[5b] Navigating to console for proper origin...")
    try:
        page.goto("https://www.codebuddy.ai/console/accounts", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception as e:
        print(f"    Navigation warning: {e}")
    time.sleep(2)
    print(f"    Now on: {page.url[:120]}")

    print("[6] Setting region + activating trial...")
    activate_result = page.evaluate("""async () => {
        try {
            const r1 = await fetch('/console/login/account', {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                },
                body: JSON.stringify({
                    attributes: {
                        countryCode: ['65'],
                        countryFullName: ['Singapore'],
                        countryName: ['SG'],
                    }
                }),
            });
            const t1 = await r1.text();

            const r2 = await fetch('/billing/ide/trial', {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                },
            });
            const t2 = await r2.text();

            return {
                region: { status: r1.status, body: t1 },
                trial:  { status: r2.status, body: t2 },
            };
        } catch (err) {
            return { error: String(err) };
        }
    }""")
    if "error" in activate_result:
        print(f"    ERROR: {activate_result['error']}")
    else:
        print(f"    region -> HTTP {activate_result['region']['status']}: {activate_result['region']['body'][:200]}")
        print(f"    trial  -> HTTP {activate_result['trial']['status']}: {activate_result['trial']['body'][:200]}")

    # Reload to get fresh page state (ignore errors — page may be mid-redirect)
    try:
        page.goto("https://www.codebuddy.ai/console/accounts", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception as e:
        print(f"    Navigation warning: {e}")
        time.sleep(3)

    time.sleep(2)

    restricted = page.locator("text=Account Access Restricted, text=temporarily restricted")
    if restricted.count() > 0 or "restricted" in page.content().lower()[:5000]:
        print("[6b] Still restricted after activation. Trying /console/login/enterprise...")
        enterprise_result = page.evaluate("""async (state) => {
            try {
                const resp = await fetch('https://www.codebuddy.ai/console/login/enterprise?state=' + state, {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        'Accept': 'application/json',
                        'Content-Type': 'application/json',
                    },
                });
                const text = await resp.text();
                return { status: resp.status, body: text };
            } catch (err) {
                return { status: 0, body: String(err) };
            }
        }""", state)
        print(f"    /console/login/enterprise -> HTTP {enterprise_result['status']}")
        print(f"    Body: {enterprise_result['body'][:300]}")
        try:
            page.goto("https://www.codebuddy.ai/console/accounts", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_load_state("networkidle", timeout=15000)
        except:
            time.sleep(3)
    else:
        print("[6b] Not restricted. Good.")

    print("\n[7] Fetching /console/accounts...")
    accounts_result = page.evaluate("""async () => {
        try {
            const resp = await fetch('https://www.codebuddy.ai/console/accounts', {
                credentials: 'include',
                headers: { 'Accept': 'application/json' },
            });
            const text = await resp.text();
            return { status: resp.status, body: text };
        } catch (err) {
            return { status: 0, body: String(err) };
        }
    }""")
    print(f"    HTTP {accounts_result['status']}")
    print(f"    {accounts_result['body'][:300]}")

    if accounts_result["status"] == 200:
        print("\n[8] Creating API key...")
        apikey_result = page.evaluate("""async () => {
            try {
                const resp = await fetch('https://www.codebuddy.ai/console/api/client/v1/api-keys', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        'Accept': 'application/json, text/plain, */*',
                        'Content-Type': 'application/json',
                        'X-Requested-With': 'XMLHttpRequest',
                    },
                    body: JSON.stringify({
                        name: 'kiro-' + Date.now(),
                        expire_in_days: -1,
                        user_enterprise_id: 'personal-edition-user-id',
                    }),
                });
                const text = await resp.text();
                return { status: resp.status, body: text };
            } catch (err) {
                return { status: 0, body: String(err) };
            }
        }""")
        print(f"    HTTP {apikey_result['status']}")
        print(f"    {apikey_result['body'][:500]}")

        if apikey_result["status"] == 200:
            data = json.loads(apikey_result["body"])
            key = data.get("data", {}).get("key", "")
            if key:
                print(f"\n    API KEY: {key}")
                with open("api_key.txt", "w") as f:
                    f.write(key)
                print("    Saved to api_key.txt")

    # Test chat endpoint with the API key
    if os.path.exists("api_key.txt"):
        with open("api_key.txt") as f:
            test_key = f.read().strip()
        print(f"\n[9] Testing /v2/chat/completions with key {test_key[:20]}...")
        chat_result = page.evaluate("""async (apiKey) => {
            try {
                const resp = await fetch('https://www.codebuddy.ai/v2/chat/completions', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': 'Bearer ' + apiKey,
                    },
                    body: JSON.stringify({
                        model: 'claude-opus-4.6',
                        messages: [
                            {role: 'system', content: 'You are helpful.'},
                            {role: 'user', content: 'Say hi in 3 words'}
                        ],
                        max_tokens: 32000,
                        stream: true,
                    }),
                });
                const text = await resp.text();
                return { status: resp.status, body: text };
            } catch (err) {
                return { status: 0, body: String(err) };
            }
        }""", test_key)
        print(f"    HTTP {chat_result['status']}")
        print(f"    {chat_result['body'][:500]}")

    cookies = page.context.cookies()
    with open("cookies.json", "w", encoding="utf-8") as f:
        json.dump(cookies, f, indent=2)
    print(f"\n[10] Saved {len(cookies)} cookies to cookies.json")
    print("DONE.")
