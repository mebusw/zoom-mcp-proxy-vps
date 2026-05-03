# Zoom MCP Proxy (VPS版) - 支持User-Managed OAuth + 自动token刷新

ZOOM MCP必须要一个公网IP作为回调地址。如果用openclaw本机暴露端口（比如ngrok或DMZ）不安全，所以需要一个云服务器来中转。

Listen: http://0.0.0.0:18793/mcp

使用说明：
  1. 到ZOOM后台marketplace创建 user-managed APP,
  2. 填入 CLIENT_ID / CLIENT_SECRET / REDIRECT_URI
  3. 首次运行会打印授权URL，浏览器打开授权后粘贴返回的code
  4. 之后自动管理token刷新

部署到VPS时：
  - bind改为"0.0.0.0"以便外部访问（或配合nginx反代）
  - 建议用systemd管理进程
  - 生产环境建议用nginx套一层SSL
