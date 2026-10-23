# 部署指南

本文档说明如何把「文献速读 Agent」部署到生产环境:

- **前端**(Vue 3 + Vite)→ **Vercel**(静态托管 + `/api` 反向代理)
- **后端**(FastAPI)→ **Render**(Docker Web Service)

---

## 一、部署后端到 Render

### 1. 创建服务(两种方式任选)

**方式 A — Blueprint(推荐,全自动)**
1. 登录 <https://dashboard.render.com>
2. 右上 **New → Blueprint**
3. 连接你的 GitHub,选择 `spr-nino/paper-reader-agent` 仓库
4. Render 会读取根目录 `render.yaml`,自动创建名为 `paper-reader-backend` 的服务
5. 点 **Apply**

**方式 B — 手动 New Web Service**
1. **New → Web Service** → 选 `paper-reader-agent` 仓库
2. **Runtime** 选 `Docker`(Render 自动发现根目录 `Dockerfile`)
3. **Instance Type** 选 `Free`
4. **Health Check Path** 填 `/healthz`

### 2. 配置环境变量

在 Render 服务页 → **Environment** 填:

| Key | Value | 说明 |
|---|---|---|
| `READER_ENV` | `production` | 必填,启用生产安全检查 |
| `OPENAI_API_KEY` | `sk-xxxx...` | 你的 DeepSeek key(**用新的,不要用泄露过的那个**) |
| `OPENAI_BASE_URL` | `https://api.deepseek.com/v1` | 默认已填 |
| `OPENAI_MODEL` | `deepseek-v4-pro` | 默认已填 |
| `READER_API_TOKEN` | 随机字符串 | 前端访问令牌,自己生成一个长的(如 `openssl rand -hex 32`) |

> `render.yaml` 里 `READER_API_TOKEN` 用了 `generateValue: true`,Render 会自动生成一个,你在 **Environment** 页复制下来即可。

### 3. 记下后端 URL

部署成功后,Render 会分配 URL,形如:
```
https://paper-reader-backend.onrender.com
```
如果名字被占,Render 会加随机后缀,例如 `paper-reader-backend-abc123.onrender.com`。**以实际为准**。

访问 `https://你的后端/healthz`,返回 `{"status":"ok"}` 即成功。

---

## 二、部署前端到 Vercel

### 1. 更新后端地址(如果 Render URL 与默认不同)

打开 [`web/vercel.json`](web/vercel.json),把 `destination` 里的域名改成你实际的 Render URL:

```json
{
  "rewrites": [
    {
      "source": "/api/:path*",
      "destination": "https://你的实际后端.onrender.com/api/:path*"
    }
  ]
}
```

改完 `git commit` + `git push`。

### 2. 部署

在项目根目录执行:

```bash
cd web
vercel --prod
```

首次会询问几个问题,按下面回答:

| 问题 | 回答 |
|---|---|
| Set up and deploy? | `Y` |
| Which scope? | 选你的账号 |
| Link to existing project? | `N` |
| Project name | `paper-reader-agent` (或你想要的) |
| In which directory is your code? | `./` |
| Override settings? | `N`(Vercel 自动识别 Vite) |

部署完成后,Vercel 会输出生产 URL,形如 `https://paper-reader-agent.vercel.app`。

---

## 三、首次使用

1. 打开 Vercel 分配的前端 URL
2. 点右上 **设置**,把 Render 环境变量里的 `READER_API_TOKEN` 粘贴到 **X-Access-Token** 输入框
3. 上传一份 PDF,确认能正常解析(首次请求 Render 可能冷启动 ~50 秒,耐心等)

---

## 已知限制(Render 免费层)

- **15 分钟无请求会休眠**,下次请求冷启动约 50 秒
- **`data/` 目录不持久**:服务重启 / 重新部署后,上传过的文档和解析结果会丢。演示场景可接受;要持久化需升级 Render 付费版加 Persistent Disk,或改用对象存储
- **Word(`.doc/.docx`)转换不可用**:Render 是 Linux,没有 Microsoft Word COM。用户上传 Word 会看到引导「另存为 PDF」(代码里已经做了优雅降级)

---

## 本地开发不受影响

根目录的 `start-agent.bat / .ps1 / .sh` 依然是本地一键启动方式,不需要任何改动。
