import asyncio
import json
import sys

sys.path.insert(0, 'C:/Users/User/unifiedme-ai')

from unified import database as db


async def main() -> None:
    await db.init_db()
    sessions = await db.get_chat_sessions()
    rows = [s for s in sessions if s.get('opencode_session_key')]
    rows = sorted(rows, key=lambda s: s.get('updated_at', ''), reverse=True)[:5]
    for s in rows:
        print(json.dumps({k: s.get(k) for k in ('id', 'title', 'model', 'opencode_session_key', 'last_gumloop_account_id', 'updated_at')}, ensure_ascii=False))
        msgs = await db.get_chat_messages(s['id'])
        print('MSGCOUNT', len(msgs))
        for m in msgs[-10:]:
            content = (m.get('content', '')[:120]).replace('\n', ' ')
            print(' ', m.get('id'), m.get('role'), content)
        summary = await db.get_session_summary(s['id'])
        summary_text = ((summary or {}).get('summary_text', '')[:300]).replace('\n', ' | ')
        print('SUMMARY', (summary or {}).get('watermark_message_id'), summary_text)
        print('---')


if __name__ == '__main__':
    asyncio.run(main())
