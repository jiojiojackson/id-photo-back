# DEV_STATE

当前开发分支：`agent/queue-worker-bridge`

本仓库是证件照 GPU 推理后端，对应前端 `id-photo-front` 的 Vercel + Neon + R2 + Vercel Queue 架构。

## 当前目标

Lightning 作为无状态 GPU Worker：

1. Vercel 用户点击“开始处理”后创建 `worker_run` 和短期 Worker Credential。
2. 生产模式：Vercel 使用平台提供的 `LIGHTNING_API_URL` / `LIGHTNING_API_KEY` 唤醒 Lightning。
3. 调试模式：Vercel 直接 POST Lightning Studio Linux 服务器上的 FastAPI `/process-queue`，不使用 Lightning 平台 API Key。
4. Lightning 收到 `worker_run_id`、动态 Vercel `vercel_origin`、短期 `worker_credential` 后，通过 Bearer Credential 访问 Vercel Bridge。
5. 一个 Worker Run 内模型只加载一次，Job 严格串行处理。
6. `/api/worker/next` claim Job 后才返回 R2 input/output presigned URL。
7. 推理期间每 60 秒 heartbeat，初始 lease 为 10 分钟。
8. 成功调用 `/api/worker/complete`；失败调用 `/api/worker/fail`。
9. `/api/worker/next` 返回 `empty` 后调用 `/api/worker/finish`，Worker Run 结束。

## 已完成

- 保留原 `/generate` 同步 API，便于单张图片手动测试。
- `api_server.py` 改为 Vercel Bridge Worker contract。
- Lightning Worker 使用短期 Worker Credential，不保存 Credential，不读取任何 `LIGHTNING_*` 项目环境变量。
- Wake payload 使用稳定 snake_case：`worker_run_id`、`bridge_url`、`vercel_origin`、`worker_credential`、`worker_credential_expires_at`。
- Bridge 请求统一使用 `Authorization: Bearer <worker_credential>`。
- `/api/worker/next` 使用 POST。
- `/api/worker/heartbeat` 延长当前 Job lease。
- `/api/worker/complete` 完成 Job。
- `/api/worker/fail` 失败 Job，并由 Vercel 根据 `MAX_ATTEMPTS=5` 决定重新排队或最终失败。
- `/api/worker/finish` 结束 Worker Run。
- 每个 Job 只启动一个 heartbeat 线程；GPU inference 本身仍由 `inference_lock` 严格串行。
- 输出使用 Job 返回的 R2 presigned PUT URL 写入 PNG。
- 单个 Job 失败不会停止整个 Worker Run；会回调 fail 后继续处理下一个 Job。
- Lightning Worker 会在调用 Vercel Bridge 前输出完整请求 URL，包括协议、主机名、端口和路径；不会输出 Worker Credential。

## 2026-08-15 最新联合调试发现

收到日志：

```text
bridge_url=https://<preview-host>/api/worker
Vercel request ... url=https://<preview-host>/api/worker/api/worker/next
```

确认后端之前错误地把已经包含 `/api/worker` 的 `bridge_url` 再次拼接 `/api/worker/*`，造成：

```text
/api/worker/api/worker/next
```

现已修复为：

```text
bridge_url=https://<preview-host>/api/worker
next=https://<preview-host>/api/worker/next
heartbeat=https://<preview-host>/api/worker/heartbeat
complete=https://<preview-host>/api/worker/complete
fail=https://<preview-host>/api/worker/fail
finish=https://<preview-host>/api/worker/finish
```

因此 `bridge_url` 的语义现在明确为 **`/api/worker` 基础 URL**，各操作只追加 `/next`、`/heartbeat` 等最后一级 path。

### 第二个问题：Vercel Deployment Protection

修复 path 后，上一轮日志同时确认：

```text
401 Protected deployment
vercel_auth_enabled=true
```

这意味着 Lightning 请求在到达 Next.js `/api/worker/next` Route Handler 之前，就被 Vercel Preview Deployment Protection 拦截。

因此此前：

```text
401 → worker credential is invalid or expired
```

的诊断是不准确的。该 401 实际可能来自 Vercel Deployment Protection，而不是 Worker Credential。

后端现已解析 Vercel 401 response：

```text
reason=vercel_deployment_protection
```

或：

```text
reason=worker_credential_rejected
```

并分别输出不同错误信息。

## 当前调试方案：Lightning Studio Linux 直接运行

调试阶段：

```text
Vercel
  │
  │ POST /process-queue
  │ 无 Lightning API Key
  ▼
Lightning Studio Linux
  │
  │ FastAPI
  │
  ├─ /process-queue
  └─ Worker thread
       │
       └─ POST Vercel /api/worker/*
```

## Vercel Bridge URL

Worker 现在打印：

```text
POST https://<preview-host>/api/worker/next
POST https://<preview-host>/api/worker/heartbeat
POST https://<preview-host>/api/worker/complete
POST https://<preview-host>/api/worker/fail
POST https://<preview-host>/api/worker/finish
```

不会打印 Credential。

## Vercel Preview Protection 待处理

当前调试 Preview 开启了 Vercel Deployment Protection。Lightning Studio 没有 Vercel SSO Cookie，因此不能直接访问受保护 Preview 的 `/api/worker/*`。

目标架构仍然是：

```text
普通 Preview 页面 → Vercel Deployment Protection
/api/worker/* → 使用短期 Worker Credential 自己认证
```

因此下一步需要在 Vercel 侧为 Debug Preview 解决 Deployment Protection 对 Worker Bridge 的拦截。不要通过给 Lightning 传递用户 SSO Cookie 的方式解决。

## 启动服务

在 Lightning Studio Linux 服务器上，直接运行，不使用 Docker：

```bash
cd /path/to/id-photo-back
python3 -m pip install -r requirements.txt
python3 -m pip install "fastapi[standard]" python-multipart pillow
python3 -m uvicorn api_server:app --host 0.0.0.0 --port 8000
```

如果依赖已经安装，只需要：

```bash
cd /path/to/id-photo-back
python3 -m uvicorn api_server:app --host 0.0.0.0 --port 8000
```

服务端口：`8000`。

## 当前待验证

1. Vercel Debug Preview 解除/绕过 Deployment Protection 对 `/api/worker/*` 的拦截。
2. 重新启动 Lightning Worker。
3. 日志确认：

```text
POST https://<preview-host>/api/worker/next
```

而不是 `/api/worker/api/worker/next`。

4. 确认 `/next` 返回 200，而不是 Vercel Protected deployment 401。
5. 如果仍为 401，确认日志中的 `reason` 是 `worker_credential_rejected` 后再检查 Credential。
6. 完成 1 Job：claim → R2 input → inference → R2 output → complete。
7. 再完成 3 Job 串行测试。
8. heartbeat、Worker 崩溃、lease recovery、重复 complete、fail-retry、credential expiry。
9. 调试链路稳定后，再切回 Lightning 平台 Wake 模式。

## 当前提交

后端最新 commit：`e6e550c681ef3809e31a6909e32066c06d0dc8bd`。
