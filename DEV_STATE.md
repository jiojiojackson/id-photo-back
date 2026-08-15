# DEV_STATE

当前开发分支：`agent/queue-worker-bridge`

本仓库是证件照 GPU 推理后端，对应前端 `id-photo-front` 的 Vercel + Neon + R2 + Vercel Queue 架构。

## 当前目标

Lightning 作为无状态 GPU Worker：

1. Vercel 用户点击“开始处理”后创建 `worker_run` 和短期 Worker Credential。
2. 生产模式：Vercel 使用平台提供的 `LIGHTNING_API_URL` / `LIGHTNING_API_KEY` 唤醒 Lightning。
3. 调试模式：Vercel 直接 POST Lightning Studio Linux 服务器上的 FastAPI `/process-queue`，不使用 Lightning 平台 API Key。
4. Lightning 收到 `worker_run_id`、`bridge_url`、短期 `worker_credential` 后，通过 Bearer Credential 访问 Vercel Bridge。
5. 一个 Worker Run 内模型只加载一次，Job 严格串行处理。
6. `/api/worker/next` claim Job 后才返回 R2 input/output presigned URL。
7. 推理期间每 60 秒 heartbeat，初始 lease 为 10 分钟。
8. 成功调用 `/api/worker/complete`；失败调用 `/api/worker/fail`。
9. `/api/worker/next` 返回 `empty` 后调用 `/api/worker/finish`，Worker Run 结束。

## 已完成

- 保留原 `/generate` 同步 API，便于单张图片手动测试。
- `api_server.py` 改为 Vercel Bridge Worker contract。
- Lightning Worker 使用短期 Worker Credential，不保存 Credential，不读取任何 `LIGHTNING_*` 项目环境变量。
- Wake payload 使用稳定 snake_case：`worker_run_id`、`bridge_url`、`worker_credential`、`worker_credential_expires_at`。
- Bridge 请求统一使用 `Authorization: Bearer <worker_credential>`。
- `/api/worker/next` 使用 POST。
- `/api/worker/heartbeat` 延长当前 Job lease。
- `/api/worker/complete` 完成 Job。
- `/api/worker/fail` 失败 Job，并由 Vercel 根据 `MAX_ATTEMPTS=5` 决定重新排队或最终失败。
- `/api/worker/finish` 结束 Worker Run。
- 每个 Job 只启动一个 heartbeat 线程；GPU inference 本身仍由 `inference_lock` 严格串行。
- 输出使用 Job 返回的 R2 presigned PUT URL 写入 PNG。
- 单个 Job 失败不会停止整个 Worker Run；会回调 fail 后继续处理下一个 Job。
- Lightning Worker 现在会在调用 Vercel Bridge 前输出完整请求 URL，包括协议、主机名、端口和路径；不会输出 Worker Credential。

## 联合调试进度

### Lightning 平台 Wake

已确认生产平台 Wake 路径曾错误指向 `/`，导致：

```text
POST / 405 Method Not Allowed
```

前端现已统一调用：

```text
POST ${LIGHTNING_API_URL}/process-queue
```

随后已确认平台侧：

```text
POST /process-queue 200 OK
```

说明 FastAPI Worker endpoint 本身正常。

### Worker Credential 401

平台 Wake 成功后，Worker 调用 Vercel `/api/worker/next` 得到 401：

```text
[QueueWorker] stopped unexpectedly run=22647145-611a-4856-be52-45abffca0f00: worker credential is invalid or expired
[QueueWorker] stopped run=22647145-611a-4856-be52-45abffca0f00 processed=0
```

此前已增加 `/next` 401 response body 诊断日志，但由于 Lightning 平台部署/推理 API 联调速度较慢，当前暂不继续在平台模式排查。

## 当前调试方案：Lightning Studio Linux 直接运行

调试阶段改为：

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
       └─ POST Vercel /api/worker/next
```

### Vercel Bridge URL 诊断

Worker 会对以下所有 Bridge 请求打印完整 URL：

```text
POST <bridge_url>/api/worker/next
POST <bridge_url>/api/worker/heartbeat
POST <bridge_url>/api/worker/complete
POST <bridge_url>/api/worker/fail
POST <bridge_url>/api/worker/finish
```

日志格式例如：

```text
[QueueWorker] Vercel request stage=next method=POST url=https://id-photo-front.vercel.app/api/worker/next run=<run-id>
```

这样可以直接确认：

1. hostname 是否是正确的 Vercel 项目域名；
2. 是否误指向 Preview/旧部署域名；
3. 是否存在错误的 path；
4. 是否存在错误的协议或端口。

不会打印 `worker_credential`、Authorization header 或其他 secret。

### 启动服务

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

需要将 Lightning Studio 的公网 URL 指向这个 FastAPI 服务。Vercel 会自动调用其 `/process-queue` 路径。

## 当前重要约定

### Queue / Job ownership

Queue message 不是 GPU 任务生命周期的 source of truth。Vercel Neon Job 的 `worker_run_id + lease_expires_at` 才是任务所有权。Worker 崩溃后 lease 到期，下一次 Worker Run 可以重新 claim。

### 模型生命周期

禁止每个 Job 重新初始化模型。`IDCreator()` 在进程启动时初始化，一个 Worker Run 内连续处理多个 Job。

### Worker 入口

FastAPI endpoint：

```text
POST /process-queue
```

请求成功只代表 Worker thread 已启动，不代表 Job 已完成。实际结果通过 Bridge `next/complete/fail/finish` 回写 Vercel。

## 当前待验证

1. Lightning Studio 直接运行 FastAPI 后，Vercel → `/process-queue` 是否稳定返回 200。
2. 从后端日志确认完整 Vercel Bridge hostname/path 正确。
3. Worker → Vercel `/api/worker/next` 的 Credential 401 是否仍存在。
4. 如果认证通过，完成 1 Job：claim → R2 input → inference → R2 output → complete。
5. 再完成 3 Job 串行测试。
6. heartbeat、Worker 崩溃、lease recovery、重复 complete、fail-retry、credential expiry。
7. 真实推理 p95 / 最大时间，之后校准 10 分钟 lease、60 秒 heartbeat 和 15 分钟 R2 URL。
8. 调试链路稳定后，再切回 Lightning 平台 Wake 模式并继续处理此前平台模式的 Credential 401。

## 当前提交

后端当前最新调试 commit：`0af3e7e0c2f3213473c09e7e024f47075dab7e8c`。
