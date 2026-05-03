# Zoom MCP Proxy (VPS版) - 支持User-Managed OAuth + 自动token刷新

ZOOM MCP必须要一个公网IP作为回调地址。如果用openclaw本机暴露端口（比如ngrok或DMZ）不安全，所以需要一个云服务器来中转。

Listen: http://0.0.0.0:18793/mcp
CallBack: http://0.0.0.0:18794/callback

## 使用说明
  1. 到ZOOM后台marketplace创建 user-managed APP
  2. 填入APP的 CLIENT_ID / CLIENT_SECRET / REDIRECT_URI 到 `zoom-mcp-proxy.py` 脚本里
  3. 首次运行会打印授权URL，浏览器打开授权后粘贴返回的code
  4. 之后自动管理token刷新
  5. 推荐，设置开机服务自启动

部署到VPS时：
  - bind改为"0.0.0.0"以便外部访问（或配合nginx反代）
  - 建议用systemd管理进程
  - 生产环境建议用nginx套一层SSL
  - 云服务器防火墙需允许“入方向”  18793和18794 端口


## 命令

```bash
# 检查健康状态 （GET方法）
curl  --max-time 8 http://39.106.173.81:18793/health

# 重新获取zoom授权 （GET方法）
curl  --max-time 8 http://39.106.173.81:18793/reauthorize

# 访问MCP服务 （POST方法）
curl  --max-time 8 -N http://39.106.173.81:18793/mcp -d '{}'
```

## 设置开机服务自启动

以 CentOS 7, python 3.6为例

将 `zoom-mcp-proxy.service` 保存为 `/etc/systemd/system/zoom-mcp-proxy.service`

用命令来控制服务启停
```
sudo systemctl restart zoom-mcp-proxy
sudo systemctl status zoom-mcp-proxy
sudo systemctl stop zoom-mcp-proxy
```
