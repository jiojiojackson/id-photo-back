# DEV_STATE

当前开发分支：`agent/queue-worker-bridge`

本仓库是证件照 GPU 推理后端，对应前端 `id-photo-front` 的 Vercel + Neon + R2 + Vercel Queue 架构。

## 当前目标

Lightning 作为无状态 GPU Worker：

1. Vercel 用户点击“开始处理”后创建 `worker_run` 和短期 Worker Credential。
2. Vercel 使用平台提供的 `LIGHTNING_API_URL` / `LIGHTNING_API_KEY` 唤醒 Lightning。
3. Lightning 收到 `worker_run_id`、`bridge_url`、短期 `worker_credential` 后，通过 Bearer Credential 访问 Vercel Bridge。
4. 一个 Worker Run 内模型只加载一次，Job 严格串行处理。
5. `/api/worker/next` claim Job 后才返回 R2 input/output presigned URL。
6. 推理期间每 60 秒 heartbeat，初始 lease 为 10 分钟。
7. 成功调用 `/api/worker/complete`；失败调用 `/api/worker/fail`。
8. `/api/worker/next` 返回 `empty` 后调用 `/api/worker/finish`，Worker Run 结束。

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

## 重要约定

### Lightning 无状态

Lightning 容器不配置项目级 `LIGHTNING_API_URL`、`LIGHTNING_API_KEY`、`DATABASE_URL`、R2、Queue 等环境变量。Vercel 负责协调和密钥管理。

### Worker Credential

Credential 只通过 Vercel → Lightning wake 请求 body 传递。Lightning 仅驻留内存，并在调用 Bridge 时作为 Bearer token 使用。

### Queue / Job ownership

Queue message 不是 GPU 任务生命周期的 source of truth。Vercel Neon Job 的 `worker_run_id + lease_expires_at` 才是任务所有权。Worker 崩溃后 lease 到期，下一次 Worker Run 可以重新 claim。

### 模型生命周期

禁止每个 Job 重新初始化模型。`IDCreator()` 在进程启动时初始化，一个 Worker Run 内连续处理多个 Job。

## 当前待验证

1. Lightning 实际公网 endpoint 是否把 wake POST body 原样交给 `/process-queue`。
2. 真实 3 Job 串行端到端测试。
3. 真实推理 p95 / 最大时间，之后校准 10 分钟 lease、60 秒 heartbeat 和 15 分钟 R2 URL。
4. Worker 崩溃后 lease recovery。
5. 重复 complete / 单 Job fail-retry / credential 过期场景。

## 当前提交

后端 worker contract 已实现于 commit `a4c16398426f8fe9e94aa9a48587c2c4a006740a`。
