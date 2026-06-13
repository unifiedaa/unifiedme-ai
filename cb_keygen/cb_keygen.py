#!/usr/bin/env python3
"""
CodeBuddy API Key Generator — Standalone
=========================================
Uses curl_cffi (Chrome TLS impersonation) to automate CodeBuddy login via Google OAuth
and extract an API key — no browser required.

Flow:
  1. POST /v2/plugin/auth/state?platform=IDE       → state + authUrl
  2. Construct Keycloak auth URL (kc_idp_hint=google) → redirect to Google
  3. Google OAuth via HTTP form submission           → capture session cookies
  4. POST /console/login/account                     → set region (Singapore)
  5. GET /console/accounts                           → extract userEnterpriseId
  6. POST /console/api/client/v1/api-keys            → create API key
  7. Save API key to codebuddy_api_key.txt

Requirements:
  pip install curl_cffi beautifulsoup4 lxml

Usage:
  # Interactive (prompts for Google email/password):
  python cb_keygen.py

  # With credentials:
  python cb_keygen.py --email user@gmail.com --password "hunter2"

  # Resume from saved cookies:
  python cb_keygen.py --cookie-file codebuddy_cookies.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING
from urllib.parse import urljoin, urlparse, parse_qs, quote

from curl_cffi import requests

# Optional HTML parsing — required for Google OAuth form handling
try:
    from bs4 import BeautifulSoup  # type: ignore[import-untyped]
    _bs4_available = True
except ImportError:
    _bs4_available = False
    if TYPE_CHECKING:
        from bs4 import BeautifulSoup  # noqa: F401

# ── logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname).1s] %(message)s",
)
log = logging.getLogger("cb_keygen")

# ── constants ────────────────────────────────────────────────────────
CODEBUDDY_BASE_URL = "https://www.codebuddy.ai"
PLATFORM = "IDE"
IMPERSONATE = "chrome120"
REQUEST_TIMEOUT = 30  # seconds

# Keycloak realm
KEYCLOAK_REALM = "copilot"
KEYCLOAK_IFRAME_SELECTOR = '/auth/realms/copilot/protocol/openid-connect/auth'

# Region (Singapore)
REGION_PAYLOAD = {
    "attributes": {
        "countryCode": ["65"],
        "countryFullName": ["Singapore"],
        "countryName": ["SG"],
    }
}

# ── endpoints ────────────────────────────────────────────────────────
ENDPOINT_STATE               = f"{CODEBUDDY_BASE_URL}/v2/plugin/auth/state?platform={PLATFORM}"
ENDPOINT_TOKEN               = f"{CODEBUDDY_BASE_URL}/v2/plugin/auth/token"
ENDPOINT_LOGIN_ACCOUNT       = f"{CODEBUDDY_BASE_URL}/console/login/account"
ENDPOINT_CONSOLE_ACCOUNTS    = f"{CODEBUDDY_BASE_URL}/console/accounts"
ENDPOINT_API_KEYS            = f"{CODEBUDDY_BASE_URL}/console/api/client/v1/api-keys"
ENDPOINT_LOGIN_ENTERPRISE    = f"{CODEBUDDY_BASE_URL}/console/login/enterprise"

# ── headers ──────────────────────────────────────────────────────────
HTML_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": CODEBUDDY_BASE_URL,
    "X-Requested-With": "XMLHttpRequest",
}

# ── helpers ──────────────────────────────────────────────────────────

def sanitize(msg: str, max_len: int = 160) -> str:
    """Truncate long strings for logging."""
    return msg if len(msg) <= max_len else msg[:max_len] + "..."


def extract_google_form_fields(html: str) -> dict[str, str]:
    """
    Extract hidden input fields from a Google sign-in form page.

    Google's sign-in pages use hidden <input> elements for flow state
    (flowName, flowEntry, continue, service, etc.). This function
    extracts them so we can submit the form via POST.
    """
    if not _bs4_available:
        # Fallback: regex-based extraction
        fields: dict[str, str] = {}
        for m in re.finditer(
            r'<input\s+[^>]*type=["\']hidden["\'][^>]*/?>', html, re.I
        ):
            name = _extract_attr(m.group(), "name")
            value = _extract_attr(m.group(), "value")
            if name:
                fields[name] = value or ""
        return fields

    soup = BeautifulSoup(html, "lxml")
    fields = {}
    for inp in soup.find_all("input", type=lambda t: t and t.lower() == "hidden"):
        name = inp.get("name")
        value = inp.get("value", "")
        if name:
            fields[name] = value if value else ""
    return fields


def _extract_attr(tag_html: str, attr: str) -> str | None:
    """Extract an HTML attribute value from a tag string using regex."""
    m = re.search(rf'{attr}\s*=\s*["\'](.*?)["\']', tag_html, re.I)
    return m.group(1) if m else None


def find_form_action(html: str) -> str | None:
    """
    Find the <form> action URL on a Google sign-in page.

    Returns the first non-empty form action, or None.
    """
    if _bs4_available:
        soup = BeautifulSoup(html, "lxml")
        for form in soup.find_all("form"):
            action = form.get("action", "").strip()
            if action:
                return action
        return None

    m = re.search(r'<form\s+[^>]*action=["\']([^"\']+)["\']', html, re.I)
    return m.group(1) if m else None



def extract_google_oauth_url(keycloak_auth_url: str, html: str) -> str | None:
    """
    When we GET the Keycloak auth URL with kc_idp_hint=google,
    it may immediately redirect (302) to Google. If it returns HTML
    (e.g. a page with a redirect button or JavaScript), we need to
    extract the Google OAuth URL from the page.

    Look for:
      1. A meta refresh tag
      2. A link with href containing accounts.google.com
      3. JavaScript window.location assignment
    """
    # Meta refresh
    m = re.search(
        r'<meta\s+[^>]*http-equiv=["\']refresh["\'][^>]*content=["\'][^;]*;\s*url=([^"\']+)["\']',
        html, re.I,
    )
    if m:
        return m.group(1).strip()

    # Link to Google
    m = re.search(
        r'href=["\'](https://accounts\.google\.com[^"\']+)["\']', html, re.I
    )
    if m:
        return m.group(1)

    # JavaScript redirect
    m = re.search(
        r'window\.location\s*=\s*["\'](https://accounts\.google\.com[^"\']+)["\']',
        html,
    )
    if m:
        return m.group(1)

    # Any link containing accounts.google.com
    m = re.search(r'(https://accounts\.google\.com[^\s"\'<>&]+)', html)
    if m:
        return m.group(1)

    return None


# ── error classes ────────────────────────────────────────────────────

class KeygenError(Exception):
    """Base error for the keygen process."""
    pass

class OAuthError(KeygenError):
    """Google OAuth flow failed."""
    pass

class APIError(KeygenError):
    """CodeBuddy API returned an unexpected response."""
    def __init__(self, msg: str, status: int = 0, body: str = ""):
        super().__init__(msg)
        self.status = status
        self.body = body


# ── main keygen class ────────────────────────────────────────────────

class CodeBuddyKeygen:
    """
    Orchestrates the CodeBuddy login + API key creation flow using
    curl_cffi with Chrome TLS fingerprint impersonation.
    """

    def __init__(
        self,
        email: str = "",
        password: str = "",
        cookie_file: str = "",
        interactive: bool = True,
    ):
        self.email = email
        self.password = password
        self.cookie_file = Path(cookie_file) if cookie_file else Path(__file__).parent / "codebuddy_cookies.json"
        self.interactive = interactive

        # Runtime state
        self.state: str = ""
        self.auth_url: str = ""
        self.user_enterprise_id: str = "personal-edition-user-id"
        self._keycloak_url: str = ""
        self._google_password_context: dict[str, Any] | None = None
        self._post_login_response: requests.Response | None = None

        # curl_cffi session with Chrome TLS fingerprint
        self.session = requests.Session()
        self.session.impersonate = IMPERSONATE
        # Start with standard browser headers
        self.session.headers.update(HTML_HEADERS)

    # ──── Phase 1: Bootstrap ───────────────────────────────────────

    def bootstrap(self) -> tuple[str, str]:
        """
        POST /v2/plugin/auth/state?platform=IDE  →  {state, authUrl}

        Returns (state, auth_url).
        """
        log.info("─" * 50)
        log.info("Phase 1: Bootstrapping CodeBuddy session")
        log.info("─" * 50)

        resp = self.session.post(
            ENDPOINT_STATE,
            json={},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Origin": CODEBUDDY_BASE_URL,
                "Referer": f"{CODEBUDDY_BASE_URL}/",
            },
            timeout=REQUEST_TIMEOUT,
        )
        log.info("  POST %s → %s", ENDPOINT_STATE, resp.status_code)

        if resp.status_code == 429:
            raise APIError("Rate limited by CodeBuddy (HTTP 429)", 429)
        if resp.status_code >= 500:
            raise APIError(f"CodeBuddy server error (HTTP {resp.status_code})", resp.status_code)
        if resp.status_code != 200:
            raise APIError(
                f"State endpoint returned HTTP {resp.status_code}: {resp.text[:200]}",
                resp.status_code, resp.text
            )

        payload = resp.json()
        if payload.get("code") != 0:
            raise APIError(
                f"State endpoint returned error code={payload.get('code')}: {payload}"
            )

        data = payload.get("data", {})
        state = (data.get("state") or "").strip()
        auth_url = (data.get("authUrl") or "").strip()

        if not state or not auth_url:
            raise APIError(f"Missing state or authUrl in response: {sanitize(str(data))}")

        self.state = state
        self.auth_url = auth_url

        log.info("  state:   %s …", state[:30])
        log.info("  authUrl: %s …", auth_url[:80])
        return state, auth_url

    # ──── Phase 2: Google OAuth via HTTP ───────────────────────────

    def _construct_keycloak_url(self) -> str:
        """
        Construct Keycloak OAuth authorization URL with kc_idp_hint=google.

        The JS SPA constructs this at runtime on `www.codebuddy.ai` domain:
          /auth/realms/copilot/protocol/openid-connect/auth?client_id=console&...
        """
        started_url = (
            f"{CODEBUDDY_BASE_URL}/started"
            f"?platform={PLATFORM}"
            f"&state={self.state}"
        )
        login_select_url = (
            f"{CODEBUDDY_BASE_URL}/login/select"
            f"?redirect_uri={quote(started_url)}"
        )
        keycloak_url = (
            f"{CODEBUDDY_BASE_URL}/auth/realms/copilot"
            f"/protocol/openid-connect/auth"
            f"?client_id=console"
            f"&response_type=code"
            f"&redirect_uri={quote(login_select_url)}"
            f"&v=2210"
            f"&product=codebuddy"
            f"&kc_idp_hint=google"
        )
        return keycloak_url

    def google_oauth(self) -> bool:
        """
        Perform the Google OAuth login purely via HTTP form submission.

        1. Construct Keycloak auth URL (kc_idp_hint=google) → GET → redirect to Google
        2. Submit email → redirect to password page
        3. Submit password → redirect through Keycloak → capture session cookies

        Returns True if session cookies were obtained.
        """
        log.info("─" * 50)
        log.info("Phase 2: Google OAuth via HTTP")
        log.info("─" * 50)

        if not self.email:
            if self.interactive:
                self.email = input("  Google email: ").strip()
            if not self.email:
                log.error("No Google email provided.")
                return False

        # ── Step 2a: Construct Keycloak URL and follow to Google ──
        keycloak_url = self._construct_keycloak_url()
        log.info("  [2a] Constructed Keycloak auth URL")
        log.debug("       URL: %s", keycloak_url[:200])

        log.info("       Following Keycloak → Google redirect …")
        resp = self.session.get(
            keycloak_url,
            headers=HTML_HEADERS,
            allow_redirects=True,
            timeout=REQUEST_TIMEOUT,
        )
        log.info("       → %s (HTTP %d)", sanitize(str(resp.url), 100), resp.status_code)

        if "accounts.google.com" not in resp.url:
            # Keycloak might have returned HTML with a meta-refresh or JS redirect
            log.warning("       Not at Google yet; scanning page for redirect …")
            google_url = extract_google_oauth_url(keycloak_url, resp.text)
            if google_url:
                log.info("       Found Google URL in page: %s …", sanitize(google_url, 80))
                resp = self.session.get(
                    google_url,
                    headers=HTML_HEADERS,
                    allow_redirects=True,
                    timeout=REQUEST_TIMEOUT,
                )
                log.info("       → %s (HTTP %d)", sanitize(str(resp.url), 100), resp.status_code)

        if "accounts.google.com" not in resp.url:
            log.error("       Could not reach Google login page. Keycloak response:")
            log.error("       %s", sanitize(resp.text[:300], 300))
            return self._interactive_oauth()

        google_html = resp.text
        google_url = resp.url
        log.info("       ✅ On Google: %s", sanitize(google_url, 100))

        # ── Step 2c: Submit email form ────────────────────────────
        if not self._submit_google_email(google_url, google_html):
            return False

        # ── Step 2d: Submit password form ─────────────────────────
        if not self._submit_google_password():
            return False

        # ── Step 2e: Handle consent / follow final redirects ──────
        log.info("  [2e] Following post-login redirects …")
        self._follow_post_login_redirects()

        # ── Step 2f: Verify we have session cookies ───────────────
        cookie_jar = self.session.cookies
        cookies_dict = dict(cookie_jar) if hasattr(cookie_jar, '__iter__') else {}
        log.info("       Session cookies: %d", len(cookies_dict))
        if cookies_dict:
            self._save_cookies(cookies_dict)
            return True

        log.warning("       No cookies captured; trying interactive mode …")
        return self._interactive_oauth()

    @staticmethod
    def _extract_google_api_params(url: str, html: str) -> dict[str, str]:
        """Extract Google sign-in API parameters from the URL and page."""
        params = {}
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        for key in ("continue", "service", "hl", "flowName", "dsh", "authuser",
                     "pstMsg", "checkedDomains", "checkConnection", "oauthState"):
            vals = qs.get(key, [])
            if vals:
                params[key] = vals[0]
        # Extract additional params from page hidden inputs
        hidden = extract_google_form_fields(html)
        params.update(hidden)
        return params

    @staticmethod
    def _find_google_signin_endpoint(html: str) -> str | None:
        """Try to find the Google sign-in API endpoint from the page."""
        # Look for patterns like '/_/signin/sl/v1/identifier' in script data
        for m in re.finditer(r'["\'](/_/signin/sl/v\d+/identifier)["\']', html):
            return m.group(1)
        # Fallback: use the well-known endpoint
        return None

    def _submit_google_email(self, google_url: str, html: str) -> bool:
        """Submit the Google email/identifier form."""
        log.info("  [2c] Submitting Google email …")

        hidden = extract_google_form_fields(html)
        form_action = find_form_action(html)
        email_field = "identifier" if "identifier" in html else "Email"

        if form_action:
            form_action = urljoin(google_url, form_action)
            log.info("       Form action found: %s …", sanitize(form_action, 80))
        else:
            # No HTML form — try Google's internal sign-in API
            signin_api = self._find_google_signin_endpoint(html)
            if signin_api:
                form_action = urljoin(google_url, signin_api)
                log.info("       Using Google sign-in API: %s …", sanitize(form_action, 80))
            else:
                form_action = google_url
                log.warning("       No form found; posting to current URL: %s …", sanitize(form_action, 80))

        post_data = {**hidden, email_field: self.email}

        log.info("       POST email (%d fields) …", len(post_data))
        resp = self.session.post(
            form_action,
            data=post_data,
            headers={
                **HTML_HEADERS,
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
            },
            allow_redirects=False,
            timeout=REQUEST_TIMEOUT,
        )
        log.info("       → HTTP %d location: %s", resp.status_code, sanitize(str(resp.headers.get("Location", "")), 100))

        # Google may respond with:
        # - 302 redirect to password page
        # - 200 OK with password form HTML (if JS-based)
        # - 302 redirect to consent page (already signed in)

        if resp.status_code in (301, 302, 303, 307, 308):
            redirect_target = resp.headers.get("Location", "")
            if "accounts.google.com" in redirect_target or redirect_target.startswith("/"):
                target = urljoin(form_action, redirect_target)
                log.info("       Following redirect to: %s …", sanitize(target, 80))
                resp2 = self.session.get(
                    target,
                    headers=HTML_HEADERS,
                    allow_redirects=False,
                    timeout=REQUEST_TIMEOUT,
                )
                self._google_password_context = {
                    "url": resp2.url if "accounts.google.com" in resp2.url else target,
                    "html": resp2.text,
                    "cookies": dict(self.session.cookies),
                }
                if self._is_password_page(resp2.text):
                    log.info("       ✅ Password page reached")
                    return True
                # Maybe it's the consent page?
                if self._is_consent_page(resp2.text):
                    log.info("       Consent page reached (handling) …")
                    return self._handle_consent_page(resp2.url, resp2.text)

                log.info("       Page reached: %s", resp2.url[:80])
                return True
            else:
                log.info("       Redirect to non-Google URL: %s", sanitize(redirect_target, 80))
                self._google_password_context = {
                    "url": redirect_target,
                    "html": "",
                    "cookies": dict(self.session.cookies),
                }
                return True

        if self._is_password_page(resp.text):
            self._google_password_context = {
                "url": resp.url if "accounts.google.com" in resp.url else google_url,
                "html": resp.text,
                "cookies": dict(self.session.cookies),
            }
            log.info("       ✅ Password page (inline)")
            return True

        if self._is_consent_page(resp.text):
            log.info("       Consent page reached instead of password")
            return self._handle_consent_page(resp.url, resp.text)

        # Maybe it's a challenge page (CAPTCHA, phone verification etc.)
        if self._is_challenge_page(resp.text):
            log.warning("       ❌ Google is showing a challenge/CAPTCHA page.")
            log.warning("       Automated login not possible. Use --interactive.")
            return False

        log.warning("       Unexpected response after email submission.")
        log.warning("       URL: %s", resp.url[:100])
        self._debug_google_page(resp.text, "post-email")
        return False

    def _submit_google_password(self) -> bool:
        """Submit the Google password via the password form."""
        ctx = self._google_password_context
        if not ctx:
            log.warning("  [2d] No password context from email step")
            return False

        url = ctx.get("url", "")
        html = ctx.get("html", "")

        if not html:
            log.info("  [2d] Re-fetching password page: %s …", sanitize(url, 80))
            resp = self.session.get(url, headers=HTML_HEADERS, timeout=REQUEST_TIMEOUT)
            html = resp.text
            url = resp.url

        if not self._is_password_page(html):
            log.warning("  [2d] Not on password page; current page: %s", sanitize(url, 80))
            if self._is_challenge_page(html):
                log.warning("       Google challenge page detected. Cannot proceed.")
                return False
            # Might already be through — check
            return True

        if not self.password:
            if self.interactive:
                import getpass
                self.password = getpass.getpass("  Google password: ")
            if not self.password:
                log.error("No Google password provided.")
                return False

        log.info("  [2d] Submitting Google password …")

        hidden = extract_google_form_fields(html)
        form_action = find_form_action(html)

        if not form_action:
            # Try a common Google password endpoint pattern
            form_action = url.replace("signin/identifier", "signin/challenge")

        form_action = urljoin(url, form_action)

        # The password field is typically "password" or "Passwd"
        password_field = "Passwd" if "Passwd" in html else "password"

        post_data = {**hidden, password_field: self.password}

        log.info("       POST password to %s …", sanitize(form_action, 80))
        resp = self.session.post(
            form_action,
            data=post_data,
            headers={**HTML_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            allow_redirects=False,
            timeout=REQUEST_TIMEOUT,
        )
        log.info("       → HTTP %d location: %s", resp.status_code, sanitize(str(resp.headers.get("Location", "")), 100))

        if resp.status_code in (301, 302, 303, 307, 308):
            redirect_target = resp.headers.get("Location", "")
            log.info("       Following redirect: %s …", sanitize(redirect_target, 80))
            # Follow redirects but without a browser, we might hit Keycloak → codebuddy://
            # Important: allow_redirects=True will follow HTTP redirects
            resp2 = self.session.get(
                urljoin(form_action, redirect_target),
                headers=HTML_HEADERS,
                allow_redirects=True,
                timeout=REQUEST_TIMEOUT,
            )
            log.info("       → %s (HTTP %d)", sanitize(str(resp2.url), 100), resp2.status_code)

            # Store the full response for redirect following
            self._post_login_response = resp2
            return True

        if self._is_consent_page(resp.text):
            log.info("       Consent page reached after password")
            return self._handle_consent_page(resp.url, resp.text)

        # Check if we got redirected to a non-Google page
        final_url = resp.url
        if "accounts.google.com" not in final_url:
            if "codebuddy" in final_url:
                log.info("       ✅ Redirected to CodeBuddy after login!")
                self._post_login_response = resp
                return True

        log.warning("       Unexpected response after password submission.")
        self._debug_google_page(resp.text, "post-password")
        return False

    def _handle_consent_page(self, url: str, html: str) -> bool:
        """Handle Google OAuth consent page (approve button)."""
        log.info("       Handling Google consent page …")

        # Look for the consent form
        hidden = extract_google_form_fields(html)
        form_action = find_form_action(html)

        if form_action:
            consent_url = urljoin(url, form_action)
        else:
            # Try the standard consent endpoint
            consent_url = url

        post_data = {**hidden}

        # Add the approve/submit parameter
        # Google consent uses various field names
        for field_name in ("submit_approve_access", "approve", "submit", "consent"):
            if field_name in html or field_name in hidden:
                post_data[field_name] = "true"
                break

        log.info("       POST consent to %s …", sanitize(consent_url, 80))
        resp = self.session.post(
            consent_url,
            data=post_data,
            headers={**HTML_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            allow_redirects=True,
            timeout=REQUEST_TIMEOUT,
        )
        log.info("       → %s (HTTP %d)", sanitize(str(resp.url), 100), resp.status_code)
        self._post_login_response = resp
        return True

    def _follow_post_login_redirects(self, max_follows: int = 10):
        """
        After Google login, follow the redirect chain through Keycloak
        and try to land on a CodeBuddy domain. Capture session cookies.
        """
        resp = self._post_login_response
        if not resp:
            return

        url = resp.url
        for i in range(max_follows):
            log.info("       Redirect step %d: %s", i + 1, sanitize(str(url), 100))

            if "codebuddy" in url.lower():
                log.info("       ✅ Landed on CodeBuddy domain!")
                # Try visiting the console accounts endpoint to solidify auth
                self._visit_started_page()
                return

            if url.startswith("codebuddy://"):
                log.info("       codebuddy:// scheme — desktop app callback (expected)")
                log.info("       Session cookies should now be available.")
                self._visit_started_page()
                return

            # Follow redirects manually
            if resp.status_code in (301, 302, 303, 307, 308):
                target = resp.headers.get("Location", "")
                if not target:
                    break
                log.info("       Following redirect: %s …", sanitize(target, 80))
                resp = self.session.get(
                    urljoin(url, target),
                    headers=HTML_HEADERS,
                    allow_redirects=False,
                    timeout=REQUEST_TIMEOUT,
                )
                url = resp.url
            else:
                break

    def _visit_started_page(self):
        """Try visiting /started?platform=IDE&state=... to finalize session."""
        started_url = f"{CODEBUDDY_BASE_URL}/started?platform={PLATFORM}"
        if self.state:
            started_url += f"&state={self.state}"
        log.info("       Visiting %s …", sanitize(started_url, 80))
        try:
            resp = self.session.get(
                started_url,
                headers=HTML_HEADERS,
                allow_redirects=True,
                timeout=REQUEST_TIMEOUT,
            )
            log.info("       → %s (HTTP %d)", sanitize(str(resp.url), 100), resp.status_code)
        except Exception as e:
            log.info("       Visit to /started: %s", e)

    # ──── Phase 2i: Interactive fallback ──────────────────────────

    def _interactive_oauth(self) -> bool:
        """
        Interactive fallback: tell the user to log in via their browser,
        then paste cookies back.
        """
        log.info("─" * 50)
        log.info("Phase 2i: Interactive OAuth")
        log.info("─" * 50)

        print()
        print("==" * 30)
        print(" Automated Google OAuth failed.")
        print()
        print(" To continue manually:")
        print(f" 1. Open this URL in your browser:")
        print(f"    {self.auth_url}")
        print(" 2. Complete the Google login")
        print(" 3. After login, copy ALL cookies for codebuddy.ai")
        print("    and paste them below as JSON")
        print()
        print(" Format: { 'cookie_name': 'value', ... }")
        print("==" * 30)
        print()

        if not self.interactive:
            log.info("  Non-interactive mode; cannot proceed with OAuth.")
            return False

        try:
            raw = input("  Paste cookies JSON (or empty to exit): ").strip()
            if not raw:
                return False
            cookies = json.loads(raw)
            for name, value in cookies.items():
                self.session.cookies.set(name, value, domain=".codebuddy.ai")
            log.info("  Loaded %d cookies into session.", len(cookies))
            return True
        except (json.JSONDecodeError, Exception) as e:
            log.error("  Failed to parse cookies: %s", e)
            return False

    # ──── Phase 2 helpers ─────────────────────────────────────────

    @staticmethod
    def _is_password_page(html: str) -> bool:
        """Check if the page contains a Google password form."""
        signals = [
            'type="password"',
            'name="password"',
            'name="Passwd"',
            "Enter your password",
            '"password"',
        ]
        return any(s in html.lower() for s in signals)

    @staticmethod
    def _is_consent_page(html: str) -> bool:
        signals = [
            "submit_approve_access",
            "consent",
            "review permissions",
            "permissions",
            "sign in with google",
        ]
        return any(s in html.lower() for s in signals)

    @staticmethod
    def _is_challenge_page(html: str) -> bool:
        signals = [
            "captcha",
            "phone verification",
            "verify your identity",
            "unusual traffic",
            "automated queries",
            "enter the code",
            "two-factor",
            "2fa",
        ]
        return any(s in html.lower() for s in signals)

    @staticmethod
    def _debug_google_page(html: str, label: str = ""):
        """Print a snippet of a Google page for debugging."""
        title = ""
        m = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
        if m:
            title = m.group(1).strip()
        # Find form-related text
        content_preview = re.sub(r'<[^>]+>', ' ', html[:2000])
        content_preview = re.sub(r'\s+', ' ', content_preview).strip()[:200]
        log.debug("  [%s] title=%s", label, sanitize(title, 80))
        log.debug("  [%s] preview=%s", label, sanitize(content_preview, 200))

    # ──── Phase 3: Set region ─────────────────────────────────────

    def set_region(self) -> bool:
        """
        POST /console/login/account with Singapore attributes.

        This must succeed before we can access /console/accounts.
        """
        log.info("─" * 50)
        log.info("Phase 3: Setting region (Singapore)")
        log.info("─" * 50)

        headers = {
            **API_HEADERS,
            "Referer": f"{CODEBUDDY_BASE_URL}/console/login/account",
        }

        resp = self.session.post(
            ENDPOINT_LOGIN_ACCOUNT,
            json=REGION_PAYLOAD,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        log.info("  POST %s → %s", ENDPOINT_LOGIN_ACCOUNT, resp.status_code)

        if resp.status_code != 200:
            log.warning("  Region endpoint returned HTTP %d: %s", resp.status_code, sanitize(resp.text[:200]))
            return False

        try:
            payload = resp.json()
        except Exception:
            log.warning("  Region response not JSON: %s", sanitize(resp.text[:200]))
            return False

        code = payload.get("code") if isinstance(payload, dict) else -1
        if code == 0:
            log.info("  ✅ Region set successfully")
            return True

        log.warning("  Region returned code=%d: %s", code, sanitize(str(payload), 200))
        # Region might already be set — continue anyway
        return True

    # ──── Phase 4: Get console accounts ───────────────────────────

    def get_console_accounts(self) -> list[dict[str, Any]]:
        """
        GET /console/accounts → extract accounts list.

        Returns list of account dicts, each with 'userEnterpriseId'.
        """
        log.info("─" * 50)
        log.info("Phase 4: Fetching console accounts")
        log.info("─" * 50)

        headers = {
            **API_HEADERS,
            "Referer": f"{CODEBUDDY_BASE_URL}/console/accounts",
        }

        resp = self.session.get(
            ENDPOINT_CONSOLE_ACCOUNTS,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        log.info("  GET %s → %s", ENDPOINT_CONSOLE_ACCOUNTS, resp.status_code)

        if resp.status_code == 401:
            log.error("  ❌ Unauthorized (HTTP 401) — session cookies invalid or expired.")
            log.error("     Try deleting codebuddy_cookies.json and re-running.")
            return []
        if resp.status_code != 200:
            log.warning("  Console accounts returned HTTP %d: %s", resp.status_code, sanitize(resp.text[:160]))
            return []

        try:
            payload = resp.json()
        except Exception:
            log.warning("  Response not JSON: %s", sanitize(resp.text[:160]))
            return []

        if payload.get("code") != 0:
            log.warning("  Console accounts returned code=%d", payload.get("code"))
            return []

        data = payload.get("data", {})
        accounts = data.get("accounts", [])

        if not accounts:
            log.warning("  No accounts found in response")
            return []

        for acct in accounts:
            eid = acct.get("userEnterpriseId", "")
            log.info("  Account: %s (id: %s …)", acct.get("name", ""), eid[:20] if eid else "N/A")

        return accounts

    # ──── Phase 5: Create API key ─────────────────────────────────

    def create_api_key(self, user_enterprise_id: str) -> str | None:
        """
        POST /console/api/client/v1/api-keys  →  create a new API key.
        Returns the key string, or None on failure.
        """
        log.info("─" * 50)
        log.info("Phase 5: Creating API key")
        log.info("─" * 50)

        key_name = f"kiro-{int(time.time())}"
        payload_body = {
            "name": key_name,
            "expire_in_days": -1,
            "user_enterprise_id": user_enterprise_id,
        }

        headers = {
            **API_HEADERS,
            "Referer": f"{CODEBUDDY_BASE_URL}/console/api/client/v1/api-keys",
        }

        resp = self.session.post(
            ENDPOINT_API_KEYS,
            json=payload_body,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        log.info("  POST %s → %s", ENDPOINT_API_KEYS, resp.status_code)

        if resp.status_code == 401:
            log.error("  ❌ Unauthorized (HTTP 401) — session expired.")
            return None
        if resp.status_code == 409:
            log.info("  API key name already exists — trying with a new timestamp")
            return self.create_api_key(user_enterprise_id)
        if resp.status_code != 200:
            log.warning("  API key creation returned HTTP %d: %s", resp.status_code, sanitize(resp.text[:200]))
            return None

        try:
            payload = resp.json()
        except Exception:
            log.warning("  Response not JSON: %s", sanitize(resp.text[:200]))
            return None

        if payload.get("code") != 0:
            log.warning("  API key creation returned code=%d: %s", payload.get("code"), sanitize(str(payload), 200))
            return None

        data = payload.get("data", {})
        api_key = str(data.get("key") or "").strip()

        if not api_key:
            log.warning("  Response missing 'key' field: %s", sanitize(str(data), 200))
            return None

        log.info("  ✅ API key created: %s …", api_key[:20])
        return api_key

    # ──── Phase 6: Save ───────────────────────────────────────────

    def save_api_key(self, api_key: str) -> Path:
        """Write the API key to codebuddy_api_key.txt."""
        out_path = Path(__file__).parent / "codebuddy_api_key.txt"
        out_path.write_text(api_key.strip() + "\n")
        log.info("─" * 50)
        log.info("✅ API key saved to: %s", out_path.resolve())
        log.info("   Key: %s", api_key)
        log.info("─" * 50)
        return out_path

    def _save_cookies(self, cookies: dict[str, str]):
        """Persist cookies for session resumption."""
        try:
            with open(self.cookie_file, "w") as f:
                json.dump(cookies, f, indent=2)
            log.info("  💾 Cookies saved to %s", self.cookie_file.resolve())
        except Exception as e:
            log.warning("  Could not save cookies: %s", e)

    def load_cookies(self) -> bool:
        """Load previously saved cookies into the session."""
        if not self.cookie_file.exists():
            return False
        try:
            with open(self.cookie_file) as f:
                cookies = json.load(f)
            for name, value in cookies.items():
                self.session.cookies.set(name, value, domain=".codebuddy.ai")
            log.info("  📂 Loaded %d cookies from %s", len(cookies), self.cookie_file.resolve())
            return True
        except Exception as e:
            log.warning("  Could not load cookies: %s", e)
            return False

    # ──── Main runner ─────────────────────────────────────────────

    def run(self) -> str | None:
        """
        Execute the full keygen flow.

        Returns the API key string, or None on failure.
        """
        # Try loading saved cookies first
        loaded = self.load_cookies()
        if loaded:
            log.info("Trying saved cookies …")
            # Quick check: try fetching accounts
            accounts = self.get_console_accounts()
            if accounts:
                eid = accounts[0].get("userEnterpriseId", "personal-edition-user-id")
                api_key = self.create_api_key(eid)
                if api_key:
                    self.save_api_key(api_key)
                    return api_key
                log.info("Saved cookies invalid for API key creation; re-authenticating.")
            else:
                log.info("Saved cookies expired. Proceeding with fresh login.")
                self.session.cookies.clear()

        try:
            # Phase 1: Bootstrap
            self.bootstrap()

            # Phase 2: Google OAuth
            if not self.google_oauth():
                log.error("❌ Google OAuth failed. Cannot proceed.")
                return None

            # Phase 3: Set region
            self.set_region()

            # Phase 4: Get console accounts
            accounts = self.get_console_accounts()
            if not accounts:
                log.error("❌ Could not fetch console accounts. Auth may have failed.")
                return None

            eid = accounts[0].get("userEnterpriseId", "personal-edition-user-id")
            self.user_enterprise_id = eid

            # Phase 5: Create API key
            api_key = self.create_api_key(eid)
            if not api_key:
                log.error("❌ Failed to create API key.")
                return None

            # Phase 6: Save
            self.save_api_key(api_key)
            return api_key

        except APIError as e:
            log.error("❌ API Error (HTTP %d): %s", e.status, e)
            return None
        except requests.RequestsError as e:
            log.error("❌ curl_cffi request error: %s", e)
            return None
        except Exception as e:
            log.error("❌ Unexpected error: %s", e)
            import traceback
            log.debug(traceback.format_exc())
            return None


# ── CLI entrypoint ─────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="CodeBuddy API Key Generator — standalone, browserless.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Interactive mode (prompts for email/password):
  python cb_keygen.py

  # With credentials:
  python cb_keygen.py --email user@gmail.com --password "secret"

  # Resume from saved cookies:
  python cb_keygen.py --cookie-file codebuddy_cookies.json
        """,
    )
    parser.add_argument("--email", help="Google account email")
    parser.add_argument("--password", help="Google account password")
    parser.add_argument("--cookie-file", help="Path to cookies JSON file")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail instead of prompting for missing input",
    )

    args = parser.parse_args()

    kg = CodeBuddyKeygen(
        email=args.email or os.getenv("GOOGLE_EMAIL", ""),
        password=args.password or os.getenv("GOOGLE_PASSWORD", ""),
        cookie_file=args.cookie_file or "",
        interactive=not args.non_interactive,
    )

    api_key = kg.run()
    if not api_key:
        sys.exit(1)


if __name__ == "__main__":
    main()
