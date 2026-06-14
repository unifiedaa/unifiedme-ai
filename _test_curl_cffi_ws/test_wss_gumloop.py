"""Test curl_cffi WebSocket to Gumloop — compare with standard websockets library.

Tests:
1. curl_cffi ws_connect with Chrome impersonation (no captcha token)
2. Standard websockets library (no captcha token)
3. Compare: which one gets blocked / needs captcha?
"""

import asyncio
import json
import sys
import time
import uuid

sys.path.insert(0, "C:/Users/User/unifiedme-ai")

from unified.gumloop.auth import GumloopAuth

WS_URL = "wss://ws.gumloop.com/ws/gummies"


async def get_auth_and_gummie():
    import sqlite3
    conn = sqlite3.connect("C:/Users/User/unifiedme-ai/unified/data/unified.db")
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        'SELECT id, email, gl_gummie_id, gl_refresh_token, gl_id_token, gl_user_id '
        'FROM accounts WHERE gl_status="ok" AND gl_gummie_id != "" AND gl_refresh_token != "" LIMIT 1'
    )
    row = cur.fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def build_payload(id_token: str, gummie_id: str, user_id: str) -> str:
    interaction_id = uuid.uuid4().hex[:22]
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    payload = {
        "type": "start",
        "payload": {
            "id_token": id_token,
            "context": {
                "gummie_id": gummie_id,
                "message": {
                    "id": msg_id,
                    "role": "user",
                    "content": "Say hello in one word.",
                    "timestamp": ts,
                },
                "chat": {
                    "id": interaction_id,
                    "msgs": [
                        {
                            "id": msg_id,
                            "role": "user",
                            "content": "Say hello in one word.",
                            "timestamp": ts,
                        }
                    ],
                },
                "interaction_id": interaction_id,
            },
        },
    }
    return json.dumps(payload)


async def test_curl_cffi(id_token: str, gummie_id: str, user_id: str):
    """Test WebSocket via curl_cffi with Chrome impersonation."""
    from curl_cffi.requests import AsyncSession

    print("\n" + "=" * 60)
    print("TEST 1: curl_cffi WebSocket (impersonate=chrome)")
    print("=" * 60)

    payload_str = build_payload(id_token, gummie_id, user_id)
    print(f"  Payload size: {len(payload_str)} bytes")
    print(f"  Gummie ID: {gummie_id}")

    start = time.time()
    try:
        async with AsyncSession(impersonate="chrome") as s:
            ws = await s.ws_connect(
                WS_URL,
                headers={"Origin": "https://www.gumloop.com"},
            )
            print(f"  [OK] WebSocket connected in {time.time()-start:.2f}s")

            await ws.send(payload_str)
            print("  [OK] Payload sent")

            events = []
            text_chunks = []
            for _ in range(100):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    # curl_cffi ws.recv() may return (bytes, int) tuple or str
                    if isinstance(raw, tuple):
                        msg = raw[0].decode("utf-8", errors="replace") if isinstance(raw[0], bytes) else str(raw[0])
                    elif isinstance(raw, bytes):
                        msg = raw.decode("utf-8", errors="replace")
                    else:
                        msg = str(raw)
                    event = json.loads(msg)
                    events.append(event)
                    etype = event.get("type", "")

                    if etype == "text":
                        chunk = event.get("text", "")
                        text_chunks.append(chunk)
                    elif etype == "error":
                        print(f"  [ERROR] {json.dumps(event, indent=2)[:500]}")
                        break
                    elif etype == "captcha_required":
                        print(f"  [CAPTCHA] {json.dumps(event)[:300]}")
                        break
                    elif etype == "finish":
                        print(f"  [FINISH] Done")
                        break
                    else:
                        print(f"  [EVENT] type={etype} | {json.dumps(event)[:200]}")
                except asyncio.TimeoutError:
                    print("  [TIMEOUT] No response in 30s")
                    break

            full_text = "".join(text_chunks)
            elapsed = time.time() - start
            print(f"  Response: \"{full_text[:200]}\"")
            print(f"  Events received: {len(events)}")
            print(f"  Total time: {elapsed:.2f}s")
            print(f"  Result: {'SUCCESS' if text_chunks else 'FAILED'}")

            await ws.close()

    except Exception as e:
        elapsed = time.time() - start
        print(f"  [EXCEPTION] {type(e).__name__}: {e}")
        print(f"  Time before failure: {elapsed:.2f}s")
        return False

    return bool(text_chunks)


async def test_websockets_lib(id_token: str, gummie_id: str, user_id: str):
    """Test WebSocket via standard websockets library (current implementation)."""
    import websockets

    print("\n" + "=" * 60)
    print("TEST 2: websockets library (no impersonation)")
    print("=" * 60)

    payload_str = build_payload(id_token, gummie_id, user_id)
    print(f"  Payload size: {len(payload_str)} bytes")
    print(f"  Gummie ID: {gummie_id}")

    start = time.time()
    try:
        ws_ver = int(str(getattr(websockets, "__version__", "13.0")).split(".")[0])
        hdr_key = "additional_headers" if ws_ver >= 13 else "extra_headers"
        ws_kwargs = {
            hdr_key: {"Origin": "https://www.gumloop.com"},
            "open_timeout": 15,
        }

        async with websockets.connect(WS_URL, **ws_kwargs) as ws:
            print(f"  [OK] WebSocket connected in {time.time()-start:.2f}s")

            await ws.send(payload_str)
            print("  [OK] Payload sent")

            events = []
            text_chunks = []
            async for message in ws:
                try:
                    event = json.loads(message)
                    events.append(event)
                    etype = event.get("type", "")

                    if etype == "text":
                        chunk = event.get("text", "")
                        text_chunks.append(chunk)
                    elif etype == "error":
                        print(f"  [ERROR] {event.get('error', event)}")
                        break
                    elif etype == "captcha_required":
                        print(f"  [CAPTCHA] Captcha required! {event}")
                        break
                    elif etype == "finish":
                        print(f"  [FINISH] Done")
                        break
                except json.JSONDecodeError:
                    continue

            full_text = "".join(text_chunks)
            elapsed = time.time() - start
            print(f"  Response: \"{full_text[:200]}\"")
            print(f"  Events received: {len(events)}")
            print(f"  Total time: {elapsed:.2f}s")
            print(f"  Result: {'SUCCESS' if text_chunks else 'FAILED'}")

    except Exception as e:
        elapsed = time.time() - start
        print(f"  [EXCEPTION] {type(e).__name__}: {e}")
        print(f"  Time before failure: {elapsed:.2f}s")
        return False

    return bool(text_chunks)


async def main():
    print("Fetching GL account from DB...")
    acct = await get_auth_and_gummie()
    if not acct:
        print("ERROR: No working GL account found")
        return

    print(f"Using account: {acct['email']}")
    print(f"Gummie ID: {acct['gl_gummie_id']}")

    auth = GumloopAuth(
        refresh_token=acct.get("gl_refresh_token", ""),
        user_id=acct.get("gl_user_id", ""),
        id_token=acct.get("gl_id_token", ""),
    )
    id_token = await auth.get_token()
    print(f"Token refreshed: {id_token[:20]}...")

    gummie_id = acct["gl_gummie_id"]
    user_id = acct.get("gl_user_id", "")

    # Test 1: curl_cffi
    result1 = await test_curl_cffi(id_token, gummie_id, user_id)

    # Small delay between tests
    await asyncio.sleep(2)

    # Test 2: websockets
    result2 = await test_websockets_lib(id_token, gummie_id, user_id)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  curl_cffi (Chrome impersonate): {'PASS' if result1 else 'FAIL'}")
    print(f"  websockets (no impersonate):    {'PASS' if result2 else 'FAIL'}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
