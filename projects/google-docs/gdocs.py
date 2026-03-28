#!/usr/bin/env python3
"""Google Docs/Drive helper for Paperclip agents.

Usage:
    python3 gdocs.py oauth-setup          # One-time: get refresh token via browser flow
    python3 gdocs.py auth-test            # Verify authentication works
    python3 gdocs.py create "Title" ["Initial content"]
    python3 gdocs.py read <doc_id>
    python3 gdocs.py append <doc_id> "Content to append"
    python3 gdocs.py replace <doc_id> "New content"
    python3 gdocs.py list [max_results]
"""

import json
import sys
import os
import time
import threading
import urllib.request
import urllib.parse
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))

# Load .env from project root
_env_path = os.path.join(PROJECT_ROOT, ".env")
if os.path.isfile(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

OAUTH_TOKEN_PATH = os.path.join(SCRIPT_DIR, "data", "oauth_token.json")
FOLDER_ID = "1pXbU19XxvZfq1QbY3bvBZtOiQ7J8G36C"

# OAuth2 client credentials (Web application type, localhost redirect)
OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
OAUTH_REDIRECT_URI = "http://localhost:8080"
OAUTH_TOKEN_URI = "https://oauth2.googleapis.com/token"
OAUTH_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents",
]


# ---------------------------------------------------------------------------
# OAuth2 token management
# ---------------------------------------------------------------------------

def load_oauth_token() -> dict | None:
    if not os.path.exists(OAUTH_TOKEN_PATH):
        return None
    with open(OAUTH_TOKEN_PATH) as f:
        return json.load(f)


def save_oauth_token(data: dict):
    os.makedirs(os.path.dirname(OAUTH_TOKEN_PATH), exist_ok=True)
    with open(OAUTH_TOKEN_PATH, "w") as f:
        json.dump(data, f, indent=2)


def refresh_access_token(refresh_token: str) -> dict:
    """Exchange refresh token for a new access token."""
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OAUTH_CLIENT_ID,
        "client_secret": OAUTH_CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(OAUTH_TOKEN_URI, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def get_access_token() -> str:
    """Return a valid access token, refreshing if necessary."""
    token_data = load_oauth_token()
    if token_data is None:
        print(
            "ERROR: No OAuth2 token found. Run 'python3 gdocs.py oauth-setup' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Use cached access token if still valid
    if token_data.get("expires_at", 0) > time.time() + 60:
        return token_data["access_token"]

    # Refresh
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        print(
            "ERROR: No refresh token in token file. Run 'python3 gdocs.py oauth-setup'.",
            file=sys.stderr,
        )
        sys.exit(1)

    new_tokens = refresh_access_token(refresh_token)
    token_data["access_token"] = new_tokens["access_token"]
    token_data["expires_at"] = time.time() + new_tokens.get("expires_in", 3600)
    # Preserve the refresh token (Google only returns a new one on first grant)
    if "refresh_token" in new_tokens:
        token_data["refresh_token"] = new_tokens["refresh_token"]
    save_oauth_token(token_data)
    return token_data["access_token"]


# ---------------------------------------------------------------------------
# One-time OAuth2 setup flow
# ---------------------------------------------------------------------------

def cmd_oauth_setup():
    """Run the one-time OAuth2 consent flow to obtain a refresh token."""
    auth_url = (
        OAUTH_AUTH_URI
        + "?" + urllib.parse.urlencode({
            "client_id": OAUTH_CLIENT_ID,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",  # force consent to always get refresh_token
        })
    )

    print("=== Google OAuth2 Setup ===")
    print()
    print("Open this URL in your browser and authorize access:")
    print()
    print(auth_url)
    print()
    print("Waiting for redirect on http://localhost:8080 ...")

    auth_code_holder = [None]
    error_holder = [None]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if "code" in params:
                auth_code_holder[0] = params["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1>Authorization successful! You can close this tab.</h1>")
            elif "error" in params:
                error_holder[0] = params["error"][0]
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(f"<h1>Error: {error_holder[0]}</h1>".encode())
            else:
                self.send_response(400)
                self.end_headers()

        def log_message(self, fmt, *args):
            pass  # suppress server log noise

    server = HTTPServer(("localhost", 8080), Handler)
    server.timeout = 120  # 2-minute timeout

    # Serve one request
    server.handle_request()
    server.server_close()

    if error_holder[0]:
        print(f"ERROR: Authorization denied: {error_holder[0]}", file=sys.stderr)
        sys.exit(1)

    if not auth_code_holder[0]:
        print("ERROR: No authorization code received (timeout?).", file=sys.stderr)
        sys.exit(1)

    # Exchange auth code for tokens
    print("Exchanging authorization code for tokens...")
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": auth_code_holder[0],
        "redirect_uri": OAUTH_REDIRECT_URI,
        "client_id": OAUTH_CLIENT_ID,
        "client_secret": OAUTH_CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(OAUTH_TOKEN_URI, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req) as resp:
            token_response = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"Token exchange failed: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)

    if "refresh_token" not in token_response:
        print(
            "ERROR: No refresh token in response. "
            "Revoke app access at https://myaccount.google.com/permissions and retry.",
            file=sys.stderr,
        )
        sys.exit(1)

    token_data = {
        "refresh_token": token_response["refresh_token"],
        "access_token": token_response["access_token"],
        "expires_at": time.time() + token_response.get("expires_in", 3600),
    }
    save_oauth_token(token_data)
    print(f"Refresh token saved to {OAUTH_TOKEN_PATH}")
    print("OAuth2 setup complete. Run 'python3 gdocs.py auth-test' to verify.")


# ---------------------------------------------------------------------------
# API helper
# ---------------------------------------------------------------------------

def api_request(url, method="GET", body=None, token=None):
    """Make an authenticated Google API request."""
    if token is None:
        token = get_access_token()

    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {token}")

    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode()

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        print(f"API error {e.code}: {error_body}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_auth_test():
    """Test authentication by listing files in the shared folder."""
    token = get_access_token()
    print(f"Auth successful. Token starts with: {token[:20]}...")
    url = (
        f"https://www.googleapis.com/drive/v3/files"
        f"?q=%27{FOLDER_ID}%27+in+parents&fields=files(id,name,mimeType)"
    )
    result = api_request(url, token=token)
    print(f"Files in shared folder: {json.dumps(result, indent=2)}")


def cmd_create(title, content=""):
    """Create a new Google Doc in the shared folder."""
    token = get_access_token()

    drive_body = {
        "name": title,
        "mimeType": "application/vnd.google-apps.document",
        "parents": [FOLDER_ID],
    }
    doc = api_request(
        "https://www.googleapis.com/drive/v3/files?fields=id,name,webViewLink",
        method="POST",
        body=drive_body,
        token=token,
    )

    doc_id = doc["id"]
    doc_url = doc.get("webViewLink", f"https://docs.google.com/document/d/{doc_id}/edit")

    if content:
        requests_body = {
            "requests": [{
                "insertText": {
                    "location": {"index": 1},
                    "text": content,
                }
            }]
        }
        api_request(
            f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate",
            method="POST",
            body=requests_body,
            token=token,
        )

    print(json.dumps({"id": doc_id, "title": title, "url": doc_url}, indent=2))


def cmd_read(doc_id):
    """Read the content of a Google Doc."""
    doc = api_request(f"https://docs.googleapis.com/v1/documents/{doc_id}")

    text_parts = []
    body = doc.get("body", {})
    for element in body.get("content", []):
        paragraph = element.get("paragraph")
        if paragraph:
            for pe in paragraph.get("elements", []):
                text_run = pe.get("textRun")
                if text_run:
                    text_parts.append(text_run.get("content", ""))

    print(json.dumps({
        "id": doc_id,
        "title": doc.get("title", ""),
        "content": "".join(text_parts),
    }, indent=2))


def cmd_append(doc_id, content):
    """Append content to the end of a Google Doc."""
    doc = api_request(f"https://docs.googleapis.com/v1/documents/{doc_id}")
    body = doc.get("body", {})
    content_elements = body.get("content", [])

    end_index = 1
    if content_elements:
        end_index = content_elements[-1].get("endIndex", 1) - 1

    requests_body = {
        "requests": [{
            "insertText": {
                "location": {"index": max(end_index, 1)},
                "text": content,
            }
        }]
    }
    api_request(
        f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate",
        method="POST",
        body=requests_body,
    )
    print(json.dumps({"status": "appended", "doc_id": doc_id}))


def cmd_replace(doc_id, content):
    """Replace all body content in a Google Doc."""
    doc = api_request(f"https://docs.googleapis.com/v1/documents/{doc_id}")
    body = doc.get("body", {})
    content_elements = body.get("content", [])

    end_index = 1
    if content_elements:
        end_index = content_elements[-1].get("endIndex", 1) - 1

    requests = []
    if end_index > 1:
        requests.append({
            "deleteContentRange": {
                "range": {"startIndex": 1, "endIndex": end_index}
            }
        })
    requests.append({
        "insertText": {
            "location": {"index": 1},
            "text": content,
        }
    })

    api_request(
        f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate",
        method="POST",
        body={"requests": requests},
    )
    print(json.dumps({"status": "replaced", "doc_id": doc_id}))


def cmd_list(max_results=20):
    """List Google Docs in the shared folder."""
    url = (
        f"https://www.googleapis.com/drive/v3/files"
        f"?q=%27{FOLDER_ID}%27+in+parents"
        f"&fields=files(id,name,mimeType,modifiedTime,webViewLink)"
        f"&orderBy=modifiedTime+desc"
        f"&pageSize={max_results}"
    )
    result = api_request(url)
    files = result.get("files", [])
    if not files:
        print("No documents in shared folder.")
    else:
        print(json.dumps(files, indent=2))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "oauth-setup":
        cmd_oauth_setup()
    elif cmd == "auth-test":
        cmd_auth_test()
    elif cmd == "create":
        if len(sys.argv) < 3:
            print("Usage: gdocs.py create <title> [content]", file=sys.stderr)
            sys.exit(1)
        title = sys.argv[2]
        content = sys.argv[3] if len(sys.argv) > 3 else ""
        cmd_create(title, content)
    elif cmd == "read":
        if len(sys.argv) < 3:
            print("Usage: gdocs.py read <doc_id>", file=sys.stderr)
            sys.exit(1)
        cmd_read(sys.argv[2])
    elif cmd == "append":
        if len(sys.argv) < 4:
            print("Usage: gdocs.py append <doc_id> <content>", file=sys.stderr)
            sys.exit(1)
        cmd_append(sys.argv[2], sys.argv[3])
    elif cmd == "replace":
        if len(sys.argv) < 4:
            print("Usage: gdocs.py replace <doc_id> <content>", file=sys.stderr)
            sys.exit(1)
        cmd_replace(sys.argv[2], sys.argv[3])
    elif cmd == "list":
        max_r = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        cmd_list(max_r)
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
