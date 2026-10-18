# 公网演示部署

目标：把 Web 控制台（Mock 模式）与 A2A 服务部署到一台 Linux 服务器（阿里云轻量应用服务器 / ECS 均可）。

## 一、上传代码

在本机项目根目录执行（把 `USER`、`HOST` 换成服务器登录信息）：

```bash
rsync -av --exclude '.venv' --exclude '.git' --exclude 'runs' --exclude 'memory' --exclude '.env' \
  ./ USER@HOST:/opt/weaveorder/
```

也可以先打包上传：

```bash
tar --exclude='.venv' --exclude='.git' --exclude='runs' --exclude='.env' -czf weaveorder-deploy.tar.gz .
scp weaveorder-deploy.tar.gz USER@HOST:/tmp/
ssh USER@HOST 'sudo mkdir -p /opt/weaveorder && sudo tar -xzf /tmp/weaveorder-deploy.tar.gz -C /opt/weaveorder'
```

## 二、安装并启动

```bash
ssh USER@HOST 'sudo bash /opt/weaveorder/deploy/install.sh'
```

脚本会：

- 安装 Python 依赖到 `/opt/weaveorder/.venv`
- 注册 `weaveorder-web.service`（:8000，`WEAVE_PUBLIC_DEMO=1`）
- 注册 `weaveorder-a2a.service`（:8001）
- 在 ufw / firewalld 启用时放行端口

## 三、阿里云防火墙

轻量应用服务器控制台 → 实例 → 防火墙 → 添加规则：

- TCP 8000（Web 控制台）
- TCP 8001（A2A，可选）

## 四、验证

```bash
curl http://HOST:8000/api/health
curl http://HOST:8001/.well-known/agent-card.json
```

公开演示模式会自动：

- 强制 Mock（不调用真实模型、不需要 API Key）
- 关闭长期记忆写入（访客不污染偏好）
- 限制每个 IP 10 分钟最多 20 次运行
- 目标长度限制 400 字

## 五、为什么必须单 worker

运行注册表、审批闸门和 SSE 事件流都在进程内内存里。`--workers 2` 会让“创建运行”落到 worker A、而审批/SSE 落到 worker B，表现为页面一直停在 WAITING APPROVAL。因此 systemd 单元固定 `--workers 1`；需要横向扩展时应先把注册表外置（Redis）。

## 六、常用运维

```bash
systemctl status weaveorder-web weaveorder-a2a
journalctl -u weaveorder-web -f
systemctl restart weaveorder-web
```

## 七、反向代理（可选）

若有域名，可用 Nginx 把 80/443 转发到 127.0.0.1:8000；A2A 需要单独域名或端口，因为它的 AgentCard 位于根路径 `/.well-known/agent-card.json`。
