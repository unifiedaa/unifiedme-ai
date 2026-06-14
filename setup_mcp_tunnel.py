#!/usr/bin/env python3
"""Setup persistent Cloudflare Named Tunnel for MCP Server.

Run this AFTER `cloudflared tunnel login` (one-time browser auth).

Usage:
    python setup_mcp_tunnel.py --domain mcp.yourdomain.com
    python setup_mcp_tunnel.py --domain mcp.yourdomain.com --tunnel-name my-mcp
    python setup_mcp_tunnel.py --domain mcp.yourdomain.com --port 9876

After setup, start MCP server with:
    python mcp_server.py --workspace /path/to/project --named-tunnel <tunnel-name>

The URL stays the same regardless of workspace path.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


CF_CONFIG_DIR = Path.home() / ".cloudflared"
DEFAULT_TUNNEL_NAME = "unifiedme-mcp"
DEFAULT_PORT = 9876


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}")
        sys.exit(1)
    return result


def check_login() -> bool:
    cert_file = CF_CONFIG_DIR / "cert.pem"
    return cert_file.exists()


def get_existing_tunnels() -> list[dict]:
    result = run(["cloudflared", "tunnel", "list", "--output", "json"], check=False)
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return []


def tunnel_exists(name: str) -> str | None:
    tunnels = get_existing_tunnels()
    for t in tunnels:
        if t.get("name") == name:
            return t.get("id", "")
    return None


def create_tunnel(name: str) -> str:
    print(f"\n  Creating tunnel '{name}'...")
    result = run(["cloudflared", "tunnel", "create", name])
    for line in result.stdout.splitlines():
        if "Created tunnel" in line:
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "tunnel" and i + 1 < len(parts):
                    tunnel_id = parts[i + 1].strip()
                    if len(tunnel_id) > 10:
                        return tunnel_id
    for f in CF_CONFIG_DIR.glob("*.json"):
        if f.stem != "cert" and len(f.stem) > 10:
            return f.stem
    return ""


def route_dns(tunnel_name: str, domain: str) -> None:
    print(f"\n  Routing DNS: {domain} -> tunnel '{tunnel_name}'...")
    run(["cloudflared", "tunnel", "route", "dns", "--overwrite-dns", tunnel_name, domain])


def write_config(tunnel_name: str, tunnel_id: str, domain: str, port: int) -> Path:
    creds_file = CF_CONFIG_DIR / f"{tunnel_id}.json"
    config_file = CF_CONFIG_DIR / "config.yml"

    config_content = f"""tunnel: {tunnel_id}
credentials-file: {creds_file}

ingress:
  - hostname: {domain}
    service: http://localhost:{port}
  - service: http_status:404
"""
    config_file.write_text(config_content, encoding="utf-8")
    print(f"\n  Config written: {config_file}")
    return config_file


def main():
    parser = argparse.ArgumentParser(description="Setup Cloudflare Named Tunnel for MCP Server")
    parser.add_argument("--domain", required=True, help="Subdomain for MCP (e.g. mcp.yourdomain.com)")
    parser.add_argument("--tunnel-name", default=DEFAULT_TUNNEL_NAME, help=f"Tunnel name (default: {DEFAULT_TUNNEL_NAME})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"MCP server port (default: {DEFAULT_PORT})")
    args = parser.parse_args()

    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║  MCP Server — Named Tunnel Setup        ║")
    print("  ╚══════════════════════════════════════════╝")
    print()

    if not check_login():
        print("  ERROR: Not logged in to Cloudflare.")
        print("  Run: cloudflared tunnel login")
        print("  (This opens a browser to authenticate)")
        sys.exit(1)

    print(f"  ✓ Cloudflare authenticated (cert.pem found)")

    existing_id = tunnel_exists(args.tunnel_name)
    if existing_id:
        print(f"  ✓ Tunnel '{args.tunnel_name}' already exists (ID: {existing_id[:12]}...)")
        tunnel_id = existing_id
    else:
        tunnel_id = create_tunnel(args.tunnel_name)
        if not tunnel_id:
            print("  ERROR: Failed to create tunnel")
            sys.exit(1)
        print(f"  ✓ Tunnel created (ID: {tunnel_id[:12]}...)")

    route_dns(args.tunnel_name, args.domain)
    print(f"  ✓ DNS routed: {args.domain} -> tunnel")

    config_path = write_config(args.tunnel_name, tunnel_id, args.domain, args.port)
    print(f"  ✓ Config written: {config_path}")

    print()
    print("  ══════════════════════════════════════════")
    print(f"  DONE! Your persistent MCP URL:")
    print(f"  https://{args.domain}/mcp")
    print()
    print(f"  Start MCP server (any workspace):")
    print(f"  python mcp_server.py --workspace C:\\path\\to\\project --named-tunnel {args.tunnel_name}")
    print()
    print(f"  Or with no-interactive mode:")
    print(f"  python mcp_server.py --workspace . --named-tunnel {args.tunnel_name} --no-interactive")
    print("  ══════════════════════════════════════════")
    print()


if __name__ == "__main__":
    main()
