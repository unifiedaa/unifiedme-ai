"""Test taozhiyu/Turnstile-Solver API → Gumloop WSS end-to-end."""

import asyncio
import json
import time
import uuid
import sqlite3
import sys

import httpx
import websockets

sys.path.insert(0, "C:/Users/User/unifiedme-ai")
from unified.gumloop.auth import GumloopAuth

SOLVER_URL = "http://localhost:5000"
SITEKEY = "0x4AAAAAACMum7HpvvFmcf2r"
SITE_URL = "https://www.gumloop.com"
ACTION = "websocket_connect"


async def solve_token() -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"{SOLVER_URL}/turnstile", json={
            "url": SITE_URL,
            "sitekey": SITEKEY,
            "action": ACTION,
        })
        data = resp.json()
        task_id = data.get("task_id")
        if not task_id:
            print(f"  Failed to create task: {data}")
            return ""

        print(f"  Task created: {task_id}")

        for i in range(30):
            await asyncio.sleep(2)
            resp = await client.get(f"{SOLVER_URL}/result", params={"id": task_id})
            result = resp.json()
            status = result.get("status")

            if status == "success":
                token = result["data"]["token"]
                elapsed = result["data"]["elapsed_time"]
                print(f"  Solved in {elapsed:.1f}s (len={len(token)})")
                return token
            elif status == "error":
                print(f"  Solver error: {result.get('error')}")
                return ""

        print("  Timeout waiting for solve")
        return ""


async def send_message(id_token: str, gummie_id: str, token: str, message: str) -> str:
    interaction_id = uuid.uuid4().hex[:22]
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    payload = json.dumps({
        "type": "start",
        "payload": {
            "id_token": id_token,
            "turnstile_token": token,
            "captcha_token": token,
            "captcha_provider": "turnstile",
            "context": {
                "gummie_id": gummie_id,
                "message": {"id": msg_id, "role": "user", "content": message, "timestamp": ts},
                "chat": {"id": interaction_id, "msgs": [{"id": msg_id, "role": "user", "content": message, "timestamp": ts}]},
                "interaction_id": interaction_id,
            },
        },
    })

    async with websockets.connect(
        "wss://ws.gumloop.com/ws/gummies",
        additional_headers={"Origin": "https://www.gumloop.com"},
        open_timeout=15,
    ) as ws:
        await ws.send(payload)
        text_parts = []
        async for msg in ws:
            event = json.loads(msg)
            etype = event.get("type", "")
            if etype in ("text", "text-delta"):
                text_parts.append(event.get("text", "") or event.get("delta", ""))
            elif etype == "error":
                return f"[ERROR] {event.get('error')}: {event.get('errorMessage', '')}"
            elif etype == "finish":
                break
        return "".join(text_parts)


async def main():
    conn = sqlite3.connect("C:/Users/User/unifiedme-ai/unified/data/unified.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT * FROM accounts WHERE email="schoen3453f@gmosel.com" LIMIT 1').fetchone()
    conn.close()

    acct = dict(row)
    auth = GumloopAuth(
        refresh_token=acct["gl_refresh_token"],
        user_id=acct.get("gl_user_id", ""),
        id_token=acct.get("gl_id_token", ""),
    )
    id_token = await auth.get_token()
    gummie_id = acct["gl_gummie_id"]

    print(f"Account: {acct['email']}")
    print(f"Gummie: {gummie_id}")
    print(f"Solver: {SOLVER_URL}")
    print()

    # Test 1: Simple message
    print("=== TEST 1: Simple hello ===")
    print("Solving token...")
    token1 = await solve_token()
    if not token1:
        print("ABORT - no token")
        return
    print("Sending to WSS...")
    resp1 = await send_message(id_token, gummie_id, token1, "Say hello in one sentence.")
    print(f"Response: {resp1[:200]}")
    print()

    # Test 2: MCP create file
    print("=== TEST 2: MCP create file ===")
    print("Solving token...")
    token2 = await solve_token()
    if not token2:
        print("ABORT - no token")
        return
    print("Sending to WSS...")
    timestamp = time.strftime("%H:%M:%S")
    resp2 = await send_message(
        id_token, gummie_id, token2,
        f"Create a file called 'solver_test.txt' with content 'taozhiyu solver works! {timestamp}' using write_file tool."
    )
    print(f"Response: {resp2[:300]}")
    print()

    # Test 3: MCP read file
    print("=== TEST 3: MCP read file ===")
    print("Solving token...")
    token3 = await solve_token()
    if not token3:
        print("ABORT - no token")
        return
    print("Sending to WSS...")
    resp3 = await send_message(id_token, gummie_id, token3, "Read the file 'solver_test.txt' and show its contents.")
    print(f"Response: {resp3[:300]}")
    print()

    # Summary
    results = [resp1, resp2, resp3]
    passed = sum(1 for r in results if r and not r.startswith("[ERROR]"))
    print(f"=== RESULT: {passed}/3 passed ===")


if __name__ == "__main__":
    asyncio.run(main())
