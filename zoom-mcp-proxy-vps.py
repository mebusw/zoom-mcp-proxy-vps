#!/usr/bin/env python3
import json, time, os, sys, http.server, socketserver, base64, secrets, hashlib, urllib.request, urllib.error, urllib.parse, threading, queue

PROXY_PORT = 18793
PROXY_HOST = "0.0.0.0"
ZOOM_MCP_URL = "https://mcp.zoom.us/mcp/zoom/streamable"

# ← 换成你的 User-Managed OAuth App 的凭据
CLIENT_ID     = "YOUR_CLIENT_ID"
CLIENT_SECRET = "YOUR_CLIENT_SECRET"

# ← 换成你的公网回调地址（VPS的IP或域名）
#    例如 http://1.2.3.4:18794/callback 或 https://your-vps.com/callback
REDIRECT_URI  = "http://YOUR_VPS_IP:18794/callback"


TOKEN_CACHE_FILE = os.path.expanduser("~/.zoom-mcp-token.json")
CODE_VERIFIER_FILE = os.path.expanduser("~/.zoom-mcp-code-verifier.txt")

cb_server_instance = None
cb_server_lock = threading.Lock()

def generate_pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge

def load_code_verifier():
    if os.path.exists(CODE_VERIFIER_FILE):
        with open(CODE_VERIFIER_FILE) as f: return f.read().strip()
    return None

def save_code_verifier(v): open(CODE_VERIFIER_FILE,"w").write(v)
def clear_token():
    if os.path.exists(TOKEN_CACHE_FILE): os.remove(TOKEN_CACHE_FILE)

def build_auth_url(challenge):
    params = urllib.parse.urlencode({"response_type":"code","client_id":CLIENT_ID,"redirect_uri":REDIRECT_URI,"code_challenge":challenge,"code_challenge_method":"S256"})
    return f"https://zoom.us/oauth/authorize?{params}"

def exchange_code_for_token(code, verifier):
    creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type":"authorization_code","code":code,"redirect_uri":REDIRECT_URI,"code_verifier":verifier}).encode()
    req = urllib.request.Request("https://zoom.us/oauth/token", data=data, headers={"Authorization":f"Basic {creds}","Content-Type":"application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
        return result["access_token"], result.get("refresh_token"), result.get("expires_in", 3600)

def refresh_access_token(refresh_token):
    creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type":"refresh_token","refresh_token":refresh_token}).encode()
    req = urllib.request.Request("https://zoom.us/oauth/token", data=data, headers={"Authorization":f"Basic {creds}","Content-Type":"application/x-www-form-urlencoded"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
        return result["access_token"], result.get("refresh_token"), result.get("expires_in", 3600)

def save_token(access_token, refresh_token, expires_in):
    expiry = int(time.time()) + expires_in - 120
    with open(TOKEN_CACHE_FILE,"w") as f: json.dump({"access_token":access_token,"refresh_token":refresh_token,"expiry":expiry}, f)

def load_cached_token():
    try:
        if os.path.exists(TOKEN_CACHE_FILE):
            with open(TOKEN_CACHE_FILE) as f: data = json.load(f)
            if time.time() < data["expiry"]: return data["access_token"], data.get("refresh_token")
    except: pass
    return None, None

def get_valid_token():
    access_token, refresh_token = load_cached_token()
    if access_token:
        try:
            with open(TOKEN_CACHE_FILE) as f: data = json.load(f)
            if time.time() < (data["expiry"] - 300): return access_token
            if refresh_token:
                print("[ZoomMCP] Token过期，刷新中...")
                access_token, refresh_token, expires_in = refresh_access_token(refresh_token)
                save_token(access_token, refresh_token, expires_in)
                print("[ZoomMCP] ✅ Token刷新成功")
                return access_token
        except Exception as e: print(f"[ZoomMCP] Token刷新失败: {e}")
    return None

class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): print(f"[OAuth CB] {args[0]}", flush=True)
    def do_GET(self):
        if not self.path.startswith("/callback"): self.send_error(404); return
        parsed = urllib.parse.urlparse(self.path); params = urllib.parse.parse_qs(parsed.query)
        code = params.get("code", [""])[0]
        if code:
            print(f"[OAuth CB] 收到授权码: {code[:20]}...", flush=True)
            verifier = load_code_verifier()
            if verifier:
                try:
                    access_token, refresh_token, expires_in = exchange_code_for_token(code, verifier)
                    save_token(access_token, refresh_token, expires_in)
                    print("[ZoomMCP] ✅ Token获取成功！", flush=True)
                except Exception as e: print(f"[ZoomMCP] ❌ Token获取失败: {e}", flush=True)
            self.send_response(200); self.send_header("Content-Type","text/html"); self.end_headers()
            self.wfile.write(b"<html><body><h1>OK - close this window</h1></body></html>")
        else: self.send_error(400)

class ZoomMCPProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, fmt, *args): print(f"[ZoomMCP] {args[0]}")
    def do_GET(self):
        global cb_server_instance
        if self.path == "/health":
            access_token, _ = load_cached_token()
            self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
            self.wfile.write(json.dumps({"status":"ok","token_cached":bool(access_token),"mcp_url":ZOOM_MCP_URL}).encode())
        elif self.path == "/reauthorize":
            clear_token()
            verifier, challenge = generate_pkce()
            save_code_verifier(verifier)
            auth_url = build_auth_url(challenge)
            # 关闭旧的回调服务器
            with cb_server_lock:
                if cb_server_instance:
                    try: cb_server_instance.shutdown()
                    except: pass
                    cb_server_instance = None
            # 启动新的回调服务器
            cb_server = socketserver.TCPServer(("0.0.0.0", 18794), OAuthCallbackHandler, bind_and_activate=False)
            cb_server.allow_reuse_address = True
            cb_server.server_bind()
            cb_server.server_activate()
            cb_thread = threading.Thread(target=cb_server.serve_forever, daemon=True)
            cb_thread.start()
            cb_server_instance = cb_server
            print("[ZoomMCP] OAuth回调服务已启动，等待授权...")
            self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
            self.wfile.write(json.dumps({"status":"reauthorize_started","auth_url":auth_url,"callback_uri":REDIRECT_URI}).encode())
        else: self.send_error(404)
    def do_POST(self):
        if self.path != "/mcp": self.send_error(404); return
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b""
        token = get_valid_token()
        if not token: self.send_error(502, "No valid token - call GET /reauthorize first"); return
        try:
            req = urllib.request.Request(ZOOM_MCP_URL, data=body, headers={"Authorization":f"Bearer {token}","Content-Type":"application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=60) as remote:
                self.send_response(remote.status)
                for h in ["Content-Type","Cache-Control","X-Content-Type-Options"]:
                    if h in remote.headers: self.send_header(h, remote.headers[h])
                self.end_headers()
                while True:
                    chunk = remote.read(4096)
                    if not chunk: break
                    self.wfile.write(chunk); self.wfile.flush()
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            self.send_error(e.code, f"Zoom MCP error: {err_body[:300]}")
        except Exception as e: self.send_error(502, f"Proxy error: {str(e)}")

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True; daemon_threads = True

def main():
    print("="*50); print("  Zoom MCP Proxy (VPS版)"); print("="*50)
    access_token, _ = load_cached_token()
    if access_token:
        print("[ZoomMCP] ✅ 已加载缓存Token")
    else:
        print("[ZoomMCP] ⚠️ 没有缓存Token，请调用 GET /reauthorize 进行首次授权")
    server = ThreadedHTTPServer((PROXY_HOST, PROXY_PORT), ZoomMCPProxyHandler)
    print(f"[ZoomMCP] MCP代理启动于 http://{PROXY_HOST}:{PROXY_PORT}/mcp")
    print(f"[ZoomMCP] 健康检查: GET /health")
    print(f"[ZoomMCP] 触发授权: GET /reauthorize")
    try: server.serve_forever()
    except KeyboardInterrupt: server.shutdown()

if __name__ == "__main__": main()
