#!/usr/bin/env python3
"""
Zoom MCP Proxy (VPS版) - 支持User-Managed OAuth + 自动token刷新
Listen: http://0.0.0.0:18793/mcp

使用说明：
  1. 填入 CLIENT_ID / CLIENT_SECRET / REDIRECT_URI
  2. 首次运行会打印授权URL，浏览器打开授权后粘贴返回的code
  3. 之后自动管理token刷新

部署到VPS时：
  - bind改为"0.0.0.0"以便外部访问（或配合nginx反代）
  - 建议用systemd管理进程
  - 生产环境建议用nginx套一层SSL
"""

import json
import time
import os
import sys
import http.server
import socketserver
import urllib.request
import urllib.error
import urllib.parse
import base64
import secrets
import hashlib

# ═══════════════════════════════════════════════════════
# 配置（部署前修改）
# ═══════════════════════════════════════════════════════
PROXY_PORT   = 18793
PROXY_HOST   = "0.0.0.0"          # 改为 "127.0.0.1" 只允许本地访问
ZOOM_MCP_URL = "https://mcp.zoom.us/mcp/zoom/streamable"

# ← 换成你的 User-Managed OAuth App 的凭据
CLIENT_ID     = "YOUR_CLIENT_ID"
CLIENT_SECRET = "YOUR_CLIENT_SECRET"

# ← 换成你的公网回调地址（VPS的IP或域名）
#    例如 http://1.2.3.4:18794/callback 或 https://your-vps.com/callback
REDIRECT_URI  = "http://YOUR_VPS_IP:18794/callback"

TOKEN_CACHE_FILE = os.path.join(os.path.expanduser("~"), ".zoom-mcp-token.json")
CODE_VERIFIER_FILE = os.path.join(os.path.expanduser("~"), ".zoom-mcp-code-verifier.txt")

# ═══════════════════════════════════════════════════════
# PKCE工具
# ═══════════════════════════════════════════════════════
def generate_pkce():
    """生成PKCE code_verifier 和 code_challenge"""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge

def load_code_verifier():
    if os.path.exists(CODE_VERIFIER_FILE):
        with open(CODE_VERIFIER_FILE) as f:
            return f.read().strip()
    return None

def save_code_verifier(verifier):
    with open(CODE_VERIFIER_FILE, "w") as f:
        f.write(verifier)

def clear_token():
    if os.path.exists(TOKEN_CACHE_FILE):
        os.remove(TOKEN_CACHE_FILE)

# ═══════════════════════════════════════════════════════
# OAuth token管理
# ═══════════════════════════════════════════════════════
def build_auth_url(code_challenge):
    """生成Zoom授权URL"""
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
    return f"https://zoom.us/oauth/authorize?{params}"

def exchange_code_for_token(code, code_verifier):
    """用授权码换取access_token"""
    creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": code_verifier,
    }).encode()

    req = urllib.request.Request(
        "https://zoom.us/oauth/token",
        data=data,
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
        return result["access_token"], result.get("refresh_token"), result.get("expires_in", 3600)

def refresh_access_token(refresh_token):
    """用refresh_token刷新access_token"""
    creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode()

    req = urllib.request.Request(
        "https://zoom.us/oauth/token",
        data=data,
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
        return result["access_token"], result.get("refresh_token"), result.get("expires_in", 3600)

def save_token(access_token, refresh_token, expires_in):
    """缓存token到文件"""
    expiry = int(time.time()) + expires_in - 120  # 提前2分钟刷新
    os.makedirs(os.path.dirname(TOKEN_CACHE_FILE) or ".", exist_ok=True)
    with open(TOKEN_CACHE_FILE, "w") as f:
        json.dump({
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expiry": expiry,
        }, f)

def load_cached_token():
    """加载缓存的token（如果未过期）"""
    try:
        if os.path.exists(TOKEN_CACHE_FILE):
            with open(TOKEN_CACHE_FILE) as f:
                data = json.load(f)
            if time.time() < data["expiry"]:
                return data["access_token"], data.get("refresh_token")
    except:
        pass
    return None, None

def get_valid_token():
    """获取有效token（优先缓存，临近过期则刷新）"""
    access_token, refresh_token = load_cached_token()

    if access_token:
        # 检查是否需要刷新（过期前5分钟）
        try:
            with open(TOKEN_CACHE_FILE) as f:
                data = json.load(f)
            if time.time() < (data["expiry"] - 300):
                return access_token
            # 临近过期，尝试刷新
            if refresh_token:
                print("[ZoomMCP] Token临近过期，尝试刷新...")
                access_token, refresh_token, expires_in = refresh_access_token(refresh_token)
                save_token(access_token, refresh_token, expires_in)
                print(f"[ZoomMCP] ✅ Token刷新成功")
                return access_token
        except Exception as e:
            print(f"[ZoomMCP] Token刷新失败: {e}")

    print("[ZoomMCP] ❌ 没有可用token，需要重新授权")
    return None

# ═══════════════════════════════════════════════════════
# OAuth回调服务器（首次授权用）
# ═══════════════════════════════════════════════════════
class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[OAuth CB] {args[0]}", flush=True)

    def do_GET(self):
        if not self.path.startswith("/callback"):
            self.send_error(404)
            return

        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        code = params.get("code", [""])[0]
        error = params.get("error", [""])[0]

        if error:
            print(f"[OAuth CB] ❌ 授权失败: {error}", flush=True)
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body><h1>Error</h1></body></html>")
            return

        if not code:
            self.send_error(400)
            return

        print(f"[OAuth CB] 收到授权码: {code[:20]}...", flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body><h1>Success! Close this window.</h1></body></html>")

        # 用code换token
        verifier = load_code_verifier()
        if verifier:
            try:
                access_token, refresh_token, expires_in = exchange_code_for_token(code, verifier)
                save_token(access_token, refresh_token, expires_in)
                print(f"[ZoomMCP] ✅ Token获取成功！有效期 {expires_in}s", flush=True)
            except Exception as e:
                print(f"[ZoomMCP] ❌ Token获取失败: {e}", flush=True)
        sys.exit(0)

def start_oauth_callback_server():
    """启动OAuth回调服务器"""
    server = socketserver.TCPServer(("0.0.0.0", 18794), OAuthCallbackHandler, bind_and_activate=False)
    server.allow_reuse_address = True
    server.server_bind()
    server.server_activate()
    print(f"[ZoomMCP] OAuth回调服务器启动于 http://0.0.0.0:18794/callback")
    return server

# ═══════════════════════════════════════════════════════
# MCP代理 Handler
# ═══════════════════════════════════════════════════════
class ZoomMCPProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"[ZoomMCP] {args[0]}")

    def do_POST(self):
        if self.path != "/mcp":
            self.send_error(404, "Not Found")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""

        token = get_valid_token()
        if not token:
            self.send_error(502, "No valid token — please re-authorize")
            return

        try:
            req = urllib.request.Request(
                ZOOM_MCP_URL,
                data=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=60) as remote:
                self.send_response(remote.status)
                for header in ["Content-Type", "Cache-Control", "X-Content-Type-Options"]:
                    if header in remote.headers:
                        self.send_header(header, remote.headers[header])
                self.end_headers()
                while True:
                    chunk = remote.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()

        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            self.send_error(e.code, f"Zoom MCP error: {err_body[:300]}")
        except Exception as e:
            self.send_error(502, f"Proxy error: {str(e)}")

    def do_GET(self):
        if self.path == "/health":
            access_token, _ = load_cached_token()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "token_cached": bool(access_token),
                "mcp_url": ZOOM_MCP_URL,
            }).encode())
        elif self.path=="/reauthorize":
            clear_token()
            verifier,challenge=generate_pkce()
            save_code_verifier(verifier)
            auth_url=build_auth_url(challenge)
            self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
            self.wfile.write(json.dumps({"status":"reauthorize","auth_url":auth_url,"callback_uri":REDIRECT_URI}).encode())
        else:
            self.send_error(404)

# ═══════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════
def main():
    print("=" * 50)
    print("  Zoom MCP Proxy (VPS版)")
    print("=" * 50)

    # 检查是否已有可用token
    access_token, _ = load_cached_token()
    if access_token:
        print("[ZoomMCP] ✅ 已有缓存token，直接启动代理")
        print("[ZoomMCP]   如需重新授权，删除 ~/.zoom-mcp-token.json")
    else:
        # 需要授权
        print("[ZoomMCP] 首次运行，需要OAuth授权...")
        verifier, challenge = generate_pkce()
        save_code_verifier(verifier)

        auth_url = build_auth_url(challenge)
        print()
        print("=" * 50)
        print("  请在浏览器打开以下URL完成授权：")
        print("=" * 50)
        print()
        print(auth_url)
        print()
        print("=" * 50)
        print(f"  回调地址: {REDIRECT_URI}")
        print("  授权服务器: http://0.0.0.0:18794/callback")
        print("=" * 50)
        print()

        # 启动回调服务器（只服务一次）
        cb_server = start_oauth_callback_server()
        print("[ZoomMCP] 等待授权完成...")
        try:
            cb_server.serve_forever()
        except KeyboardInterrupt:
            cb_server.shutdown()
            sys.exit(0)

    # 启动MCP代理
    print(f"[ZoomMCP] 启动MCP代理于 http://{PROXY_HOST}:{PROXY_PORT}/mcp")
    print(f"[ZoomMCP] 转发到 {ZOOM_MCP_URL}")
    print("[ZoomMCP] 按 Ctrl+C 停止")

    class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = ThreadedHTTPServer((PROXY_HOST, PROXY_PORT), ZoomMCPProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
        print("[ZoomMCP] 已停止")

if __name__ == "__main__":
    main()
