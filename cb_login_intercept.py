#!/usr/bin/env python3
"""CodeBuddy login via intercept flow — async Camoufox + trial activation.

Outputs JSON lines to stdout for batch_runner consumption.
Based on cb_keygen/_intercept.py flow.
"""

import argparse
import asyncio
import base64
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "cb_keygen"))

from cb_keygen import CodeBuddyKeygen

CAPTCHA_API_KEY = os.getenv("BATCHER_CAPTCHA_API_KEY", "5ff220e19373d967a494cf020fe454b7")
CAPTCHA_MAX_ATTEMPTS = int(os.getenv("BATCHER_CAPTCHA_MAX_ATTEMPTS", "5"))


def emit(data: dict):
    try:
        print(json.dumps(data), flush=True)
    except BrokenPipeError:
        pass


async def human_type(page, locator, text):
    await locator.click()
    for char in text:
        await locator.press_sequentially(char, delay=random.randint(50, 150))
        if random.random() < 0.1:
            await asyncio.sleep(random.uniform(0.1, 0.3))


async def solve_captcha_if_present(page, email: str = "") -> bool:
    for attempt in range(CAPTCHA_MAX_ATTEMPTS):
        await asyncio.sleep(1)
        if "identifier" not in page.url:
            return True
        captcha_input = page.locator("input[aria-label*='Type the text'], input[aria-label*='type the text']")
        try:
            await captcha_input.first.wait_for(state="visible", timeout=5000)
        except Exception:
            if "identifier" not in page.url:
                return True
            return False

        emit({"type": "progress", "provider": "codebuddy", "step": "captcha", "message": f"Solving captcha attempt {attempt+1}..."})

        # Re-fill email in case it was cleared after failed captcha
        if email:
            email_input = page.locator("input[type='email'], input[name='identifier']").first
            try:
                current_val = await email_input.input_value()
                if not current_val.strip():
                    await email_input.fill("")
                    await human_type(page, email_input, email)
                    await asyncio.sleep(0.5)
            except Exception:
                pass

        captcha_img = page.locator("img").filter(has_not_text="Google")
        b64 = None
        for i in range(await captcha_img.count()):
            img = captcha_img.nth(i)
            if not await img.is_visible():
                continue
            box = await img.bounding_box()
            if box and box["width"] > 50 and box["height"] > 20 and box["width"] < 400:
                b64 = base64.b64encode(await img.screenshot()).decode()
                break
        if not b64:
            b64 = base64.b64encode(await page.screenshot()).decode()

        try:
            from twocaptcha import TwoCaptcha
            solver = TwoCaptcha(CAPTCHA_API_KEY)
            result = solver.normal(b64, caseSensitive=1)
            code = result["code"]
        except Exception as exc:
            emit({"type": "debug", "message": f"Captcha solve failed: {exc}"})
            continue

        emit({"type": "progress", "provider": "codebuddy", "step": "captcha", "message": f"Captcha solved: {code}"})
        await captcha_input.first.fill("")
        await human_type(page, captcha_input.first, code)
        await asyncio.sleep(0.3)
        await page.locator("#identifierNext, button:has-text('Next')").first.click()
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(3)

    return "identifier" not in page.url


async def run_login(email: str, password: str) -> dict:
    from camoufox.async_api import AsyncCamoufox

    emit({"type": "progress", "provider": "codebuddy", "step": "bootstrap", "message": "Getting auth state..."})

    kg = CodeBuddyKeygen(email=email, password=password, interactive=False)
    try:
        state, auth_url = kg.bootstrap()
    except Exception as exc:
        return {"success": False, "error": f"Bootstrap failed: {exc}"}

    kc_url = kg._construct_keycloak_url()
    emit({"type": "progress", "provider": "codebuddy", "step": "bootstrap", "message": f"state={state[:30]}..."})

    headless = os.getenv("BATCHER_CAMOUFOX_HEADLESS", "true").lower() == "true"

    manager = AsyncCamoufox(
        headless=headless,
        geoip=False,
        os="windows",
        block_webrtc=True,
        humanize=False,
    )
    browser = await manager.__aenter__()
    page = await browser.new_page()
    page.set_default_timeout(30000)

    try:
        emit({"type": "progress", "provider": "codebuddy", "step": "navigate", "message": "Opening Keycloak..."})
        await page.goto(kc_url, wait_until="load", timeout=60000)

        # Google sign-in
        if "accounts.google.com" in page.url:
            emit({"type": "progress", "provider": "codebuddy", "step": "google_email", "message": "Filling email..."})
            await page.wait_for_selector("input[type='email'], input[name='identifier']", timeout=15000)
            email_loc = page.locator("input[type='email'], input[name='identifier']").first
            await human_type(page, email_loc, email)

            await asyncio.sleep(1)
            if not await solve_captcha_if_present(page, email):
                await page.locator("#identifierNext, button:has-text('Next')").first.click()
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                await asyncio.sleep(1)
                await solve_captcha_if_present(page, email)

            for _ in range(5):
                if "identifier" not in page.url:
                    break
                await asyncio.sleep(2)

            emit({"type": "progress", "provider": "codebuddy", "step": "google_password", "message": "Filling password..."})
            await page.wait_for_selector("input[type='password']:visible", timeout=15000)
            pw_loc = page.locator("input[type='password']:visible").first
            await human_type(page, pw_loc, password)
            await asyncio.sleep(0.5)
            await page.locator("#passwordNext, button:has-text('Next')").first.click()
            await asyncio.sleep(2)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

        # Handle consent/welcome/speedbump pages
        emit({"type": "progress", "provider": "codebuddy", "step": "consent", "message": "Handling consent pages..."})
        prev_url = ""
        for attempt in range(15):
            try:
                url = page.url
            except Exception:
                break
            if "codebuddy.ai" in url and "accounts.google" not in url:
                break
            if url == prev_url and attempt > 3:
                break
            prev_url = url

            clicked = False
            for sel in [
                "text='I understand'",
                "button:has-text('I understand')",
                "text='Continue'",
                "button:has-text('Continue')",
                "text='Accept'",
                "button:has-text('Accept')",
                "text='Allow'",
                "button:has-text('Allow')",
            ]:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click(force=True, timeout=5000)
                        clicked = True
                        await asyncio.sleep(2)
                        try:
                            await page.wait_for_load_state("networkidle", timeout=10000)
                        except Exception:
                            pass
                        break
                except Exception:
                    continue
            if not clicked:
                await asyncio.sleep(2)

        emit({"type": "progress", "provider": "codebuddy", "step": "on_codebuddy", "message": f"On CodeBuddy: {page.url[:80]}"})

        # Navigate to console for proper origin before API calls
        try:
            await page.goto("https://www.codebuddy.ai/console/accounts", wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await asyncio.sleep(2)

        # Region + trial activation
        emit({"type": "progress", "provider": "codebuddy", "step": "activate", "message": "Setting region + activating trial..."})
        activate_result = await page.evaluate("""async () => {
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
            emit({"type": "debug", "message": f"Activation error: {activate_result['error']}"})
        else:
            emit({"type": "progress", "provider": "codebuddy", "step": "activate",
                  "message": f"region={activate_result['region']['status']} trial={activate_result['trial']['status']}"})

        # Reload console
        try:
            await page.goto("https://www.codebuddy.ai/console/accounts", wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await asyncio.sleep(2)

        # Check restricted
        restricted_text = await page.evaluate("() => (document.body?.innerText || '').slice(0, 5000).toLowerCase()")
        if "restricted" in restricted_text:
            emit({"type": "progress", "provider": "codebuddy", "step": "enterprise", "message": "Trying enterprise login..."})
            await page.evaluate("""async (state) => {
                try {
                    await fetch('/console/login/enterprise?state=' + state, {
                        method: 'POST',
                        credentials: 'include',
                        headers: { 'Accept': 'application/json', 'Content-Type': 'application/json' },
                    });
                } catch(e) {}
            }""", state)
            try:
                await page.goto("https://www.codebuddy.ai/console/accounts", wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(2)

        # Fetch accounts to get userEnterpriseId
        emit({"type": "progress", "provider": "codebuddy", "step": "accounts", "message": "Fetching accounts..."})
        accounts_result = await page.evaluate("""async () => {
            try {
                const resp = await fetch('/console/accounts', {
                    credentials: 'include',
                    headers: { 'Accept': 'application/json' },
                });
                const text = await resp.text();
                return { status: resp.status, body: text };
            } catch (err) {
                return { status: 0, body: String(err) };
            }
        }""")

        user_enterprise_id = "personal-edition-user-id"
        if accounts_result["status"] == 200:
            try:
                accounts_data = json.loads(accounts_result["body"])
                accounts_list = accounts_data.get("data", {}).get("accounts", [])
                if accounts_list:
                    user_enterprise_id = accounts_list[0].get("userEnterpriseId", user_enterprise_id)
            except Exception:
                pass
        else:
            return {"success": False, "error": f"Failed to fetch accounts: HTTP {accounts_result['status']}"}

        # Create API key
        emit({"type": "progress", "provider": "codebuddy", "step": "apikey", "message": "Creating API key..."})
        apikey_result = await page.evaluate("""async (ueId) => {
            try {
                const resp = await fetch('/console/api/client/v1/api-keys', {
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
                        user_enterprise_id: ueId,
                    }),
                });
                const text = await resp.text();
                return { status: resp.status, body: text };
            } catch (err) {
                return { status: 0, body: String(err) };
            }
        }""", user_enterprise_id)

        api_key = ""
        if apikey_result["status"] == 200:
            try:
                key_data = json.loads(apikey_result["body"])
                api_key = key_data.get("data", {}).get("key", "")
            except Exception:
                pass

        if not api_key:
            return {"success": False, "error": f"API key creation failed: HTTP {apikey_result['status']} {apikey_result['body'][:200]}"}

        emit({"type": "progress", "provider": "codebuddy", "step": "apikey", "message": f"API key: {api_key[:20]}..."})

        # Capture cookies for cb_session
        cookies = await page.context.cookies()
        cookie_parts = [f"{c['name']}={c['value']}" for c in cookies if c.get("name") and c.get("value")]
        cookie_header = "; ".join(cookie_parts)

        cb_session = json.dumps({
            "cookies": cookies,
            "cookie_header": cookie_header,
            "created_at": time.time(),
            "expires_at": time.time() + 3600,
        })

        cb_interaction = json.dumps({
            "user_enterprise_id": user_enterprise_id,
            "state": state,
            "created_at": time.time(),
        })

        return {
            "success": True,
            "credentials": {
                "api_key": api_key,
                "cb_session": cb_session,
                "cb_interaction": cb_interaction,
            },
        }

    except Exception as exc:
        return {"success": False, "error": str(exc)}
    finally:
        try:
            await manager.__aexit__(None, None, None)
        except Exception:
            pass


async def main(email: str, password: str):
    emit({"type": "progress", "provider": "codebuddy", "step": "start", "message": f"Starting CodeBuddy login for {email}..."})

    result = await run_login(email, password)

    emit({"type": "result", "codebuddy": result})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    args = parser.parse_args()

    asyncio.run(main(args.email, args.password))
