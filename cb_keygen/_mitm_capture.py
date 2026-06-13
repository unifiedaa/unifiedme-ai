import json, time
from mitmproxy import http

log = []
OUTPUT = r"C:\Users\User\unifiedme-ai\cb_keygen\mitm_log.json"

def response(flow: http.HTTPFlow):
    if "codebuddy.ai" not in flow.request.pretty_host:
        return

    req_body = None
    if flow.request.content:
        try:
            req_body = flow.request.content.decode("utf-8", errors="replace")
        except:
            req_body = "(binary)"

    res_body = None
    if flow.response and flow.response.content:
        ct = flow.response.headers.get("content-type", "")
        if any(t in ct for t in ["json", "text", "html", "xml"]):
            try:
                res_body = flow.response.content.decode("utf-8", errors="replace")
                if len(res_body) > 100000:
                    res_body = res_body[:100000] + "...[TRUNCATED]"
            except:
                pass

    entry = {
        "ts": time.time(),
        "method": flow.request.method,
        "url": flow.request.pretty_url,
        "req_headers": dict(flow.request.headers),
        "req_body": req_body,
        "status": flow.response.status_code if flow.response else None,
        "res_headers": dict(flow.response.headers) if flow.response else None,
        "res_body": res_body,
    }
    log.append(entry)

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False, default=str)
