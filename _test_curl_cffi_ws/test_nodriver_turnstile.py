"""Test solving Turnstile with nodriver (undetected Chrome).

nodriver is the successor of undetected-chromedriver.
It controls Chrome directly via CDP without Selenium/webdriver,
making it invisible to Cloudflare's bot detection.
"""

import asyncio
import nodriver as uc

SITEKEY = "0x4AAAAAACMum7HpvvFmcf2r"
ACTION = "websocket_connect"
TARGET_URL = "https://www.gumloop.com"


async def solve_turnstile():
    print("Launching Chrome via nodriver...")
    browser = await uc.start(headless=False)

    print(f"Navigating to {TARGET_URL}...")
    page = await browser.get(TARGET_URL)
    await asyncio.sleep(5)

    # Inject turnstile widget
    print("Injecting turnstile widget...")
    await page.evaluate(f"""
        window._tsToken = null;
        window._tsError = null;
        const container = document.createElement('div');
        container.id = 'ts-solver';
        container.style.cssText = 'position:fixed;top:50px;left:50px;z-index:99999;background:#fff;padding:20px;border:2px solid green;';
        document.body.appendChild(container);

        if (window.turnstile) {{
            turnstile.render('#ts-solver', {{
                sitekey: '{SITEKEY}',
                action: '{ACTION}',
                callback: (token) => {{ window._tsToken = token; }},
                'error-callback': (err) => {{ window._tsError = String(err); }},
            }});
            console.log('Turnstile rendered');
        }} else {{
            // Load turnstile API first
            const s = document.createElement('script');
            s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?onload=_tsReady';
            window._tsReady = () => {{
                turnstile.render('#ts-solver', {{
                    sitekey: '{SITEKEY}',
                    action: '{ACTION}',
                    callback: (token) => {{ window._tsToken = token; }},
                    'error-callback': (err) => {{ window._tsError = String(err); }},
                }});
                console.log('Turnstile rendered (after load)');
            }};
            document.head.appendChild(s);
        }}
    """)

    await asyncio.sleep(2)
    print("Looking for turnstile iframe to click...")
    clicked = False
    for attempt in range(3):
        try:
            iframes = await page.select_all("iframe")
            for iframe in iframes:
                src = iframe.attrs.get("src", "")
                if "challenges.cloudflare.com" in src:
                    print(f"  Found turnstile iframe (attempt {attempt+1})")
                    await iframe.scroll_into_view()
                    await asyncio.sleep(0.5)
                    await iframe.mouse_click()
                    clicked = True
                    print("  Clicked!")
                    break
        except Exception as e:
            print(f"  Attempt {attempt+1} error: {e}")
        if clicked:
            break
        await asyncio.sleep(2)

    if not clicked:
        print("  Could not find/click turnstile iframe, waiting for manual solve...")

    # Wait for token
    print("Waiting for token (up to 30s)...")
    token = None
    for i in range(30):
        await asyncio.sleep(1)
        token = await page.evaluate("window._tsToken")
        if token:
            print(f"\nSUCCESS! Token solved in {i+1}s")
            print(f"Length: {len(token)}")
            print(f"Token: {token[:100]}...")

            await test_token_with_gumloop(token, page)
            break

        err = await page.evaluate("window._tsError")
        if err:
            print(f"\nTurnstile ERROR: {err}")
            break

        if (i + 1) % 5 == 0:
            print(f"  {i+1}s...")

    if not token:
        print("\nFAILED - no token after 30s")

    browser.stop()


async def _solve_new_token(page) -> str:
    """Reset turnstile widget and solve again."""
    await page.evaluate("""
        window._tsToken = null;
        window._tsError = null;
        if (window.turnstile) {
            const container = document.getElementById('ts-solver');
            if (container) { container.innerHTML = ''; }
            turnstile.render('#ts-solver', {
                sitekey: '0x4AAAAAACMum7HpvvFmcf2r',
                action: 'websocket_connect',
                callback: (t) => { window._tsToken = t; },
                'error-callback': (e) => { window._tsError = String(e); },
            });
        }
    """)
    await asyncio.sleep(2)

    try:
        iframes = await page.select_all("iframe")
        for iframe in iframes:
            src = iframe.attrs.get("src", "")
            if "challenges.cloudflare.com" in src:
                await iframe.mouse_click()
                break
    except Exception:
        pass

    for i in range(20):
        await asyncio.sleep(1)
        token = await page.evaluate("window._tsToken")
        if token:
            print(f"  Token solved in {i+1}s (len={len(token)})")
            return token
        err = await page.evaluate("window._tsError")
        if err:
            print(f"  Turnstile error: {err}")
            return ""
    print("  Token solve timeout")
    return ""


async def test_token_with_gumloop(token: str, page=None):
    import json
    import time
    import uuid
    import sqlite3
    import sys

    sys.path.insert(0, "C:/Users/User/unifiedme-ai")
    from unified.gumloop.auth import GumloopAuth

    conn = sqlite3.connect("C:/Users/User/unifiedme-ai/unified/data/unified.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        'SELECT * FROM accounts WHERE email="schoen3453f@gmosel.com" LIMIT 1'
    ).fetchone()
    conn.close()

    if not row:
        print("No GL account found")
        return

    acct = dict(row)
    auth = GumloopAuth(
        refresh_token=acct["gl_refresh_token"],
        user_id=acct.get("gl_user_id", ""),
        id_token=acct.get("gl_id_token", ""),
    )
    id_token = await auth.get_token()
    gummie_id = acct["gl_gummie_id"]

    print(f"\nTesting MULTI-MESSAGE on 1 WSS connection (account: {acct['email']})...")
    print(f"Gummie: {gummie_id}\n")

    await _test_multi_message_single_ws(id_token, gummie_id, token)


async def _test_multi_message_single_ws(id_token: str, gummie_id: str, turnstile_token: str):
    import json
    import time
    import uuid
    import websockets

    messages_to_send = [
        "Say hello in one sentence.",
        "What is 2+2? Answer in one word.",
        "Create a file called 'multi_test.txt' with content 'multi-message works' using write_file.",
    ]

    async with websockets.connect(
        "wss://ws.gumloop.com/ws/gummies",
        additional_headers={"Origin": "https://www.gumloop.com"},
        open_timeout=15,
    ) as ws:
        print(f"WSS connected. Sending {len(messages_to_send)} messages on same connection...\n")

        for idx, user_msg in enumerate(messages_to_send, 1):
            interaction_id = uuid.uuid4().hex[:22]
            msg_id = f"msg_{uuid.uuid4().hex[:24]}"
            ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            payload = json.dumps({
                "type": "start",
                "payload": {
                    "id_token": id_token,
                    "turnstile_token": turnstile_token,
                    "captcha_token": turnstile_token,
                    "captcha_provider": "turnstile",
                    "context": {
                        "gummie_id": gummie_id,
                        "message": {"id": msg_id, "role": "user", "content": user_msg, "timestamp": ts},
                        "chat": {"id": interaction_id, "msgs": [{"id": msg_id, "role": "user", "content": user_msg, "timestamp": ts}]},
                        "interaction_id": interaction_id,
                    },
                },
            })

            print(f"--- MSG {idx}: \"{user_msg[:50]}\" ---")
            await ws.send(payload)

            text_parts = []
            tool_events = []
            try:
                async for raw_msg in ws:
                    event = json.loads(raw_msg)
                    etype = event.get("type", "")
                    if etype in ("text", "text-delta"):
                        text_parts.append(event.get("text", "") or event.get("delta", ""))
                    elif etype == "error":
                        print(f"  ERROR: {event.get('error')}: {event.get('errorMessage', '')}")
                        break
                    elif etype == "finish":
                        break
                    elif "tool" in etype:
                        tool_events.append(etype)
            except Exception as e:
                print(f"  Exception during recv: {type(e).__name__}: {e}")
                break

            response = "".join(text_parts)
            if tool_events:
                print(f"  [Tools: {', '.join(set(tool_events))}]")
            if response:
                print(f"  Response: {response[:150]}")
            else:
                print("  NO RESPONSE")
                print("  (WSS likely closed or token rejected for 2nd message)")
                break
            print()

        print("Done. WSS connection closing.")


async def _send_wss(id_token: str, gummie_id: str, turnstile_token: str, message: str) -> str:
    import json
    import time
    import uuid
    import websockets

    interaction_id = uuid.uuid4().hex[:22]
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    payload = json.dumps({
        "type": "start",
        "payload": {
            "id_token": id_token,
            "turnstile_token": turnstile_token,
            "captcha_token": turnstile_token,
            "captcha_provider": "turnstile",
            "context": {
                "gummie_id": gummie_id,
                "message": {"id": msg_id, "role": "user", "content": message, "timestamp": ts},
                "chat": {"id": interaction_id, "msgs": [{"id": msg_id, "role": "user", "content": message, "timestamp": ts}]},
                "interaction_id": interaction_id,
            },
        },
    })

    try:
        async with websockets.connect(
            "wss://ws.gumloop.com/ws/gummies",
            additional_headers={"Origin": "https://www.gumloop.com"},
            open_timeout=15,
        ) as ws:
            await ws.send(payload)

            text_parts = []
            tool_events = []
            async for msg in ws:
                event = json.loads(msg)
                etype = event.get("type", "")
                if etype in ("text", "text-delta"):
                    text_parts.append(event.get("text", "") or event.get("delta", ""))
                elif etype == "error":
                    err = event.get("error", "")
                    err_msg = event.get("errorMessage", "")
                    print(f"  [ERROR] {err}: {err_msg}")
                    return ""
                elif etype == "finish":
                    break
                elif "tool" in etype:
                    tool_events.append(etype)

            if tool_events:
                print(f"  [Tools used: {', '.join(set(tool_events))}]")

            return "".join(text_parts)

    except Exception as e:
        print(f"  [Exception] {type(e).__name__}: {e}")
        return ""


if __name__ == "__main__":
    asyncio.run(solve_turnstile())
