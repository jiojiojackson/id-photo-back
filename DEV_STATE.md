# DEV_STATE

当前开发分支：`agent/queue-worker-bridge`

本仓库是证件照 CPU 推理后端，对应前端 `id-photo-front` 的 Vercel + Neon + R2 + Vercel Queue 架构。

## 当前目标

Lightning 作为无状态 Worker：

1. Vercel 用户点击“开始处理”后创建 `worker_run` 和短期 Worker Credential。
2. 生产模式：Vercel 使用平台提供的 `LIGHTNING_API_URL` / `LIGHTNING_API_KEY` 唤醒 Lightning。
3. 调试模式：Vercel 直接 POST Lightning Studio Linux 服务器上的 FastAPI `/process-queue`，不使用 Lightning 平台 API Key。
4. Lightning 收到 `worker_run_id`、动态 Vercel `vercel_origin`、短期 `worker_credential` 后，通过 Bearer Credential 访问 Vercel Bridge。
5. 一个 Worker Run 内模型只加载一次，Job 严格串行处理。
6. `/api/worker/next` claim Job 后才返回 R2 input/output presigned URL。
7. 推理期间每 60 秒 heartbeat，初始 lease 为 10 分钟。
8. 成功调用 `/api/worker/complete`；失败调用 `/api/worker/fail`。
9. `/api/worker/next` 返回 `empty` 后调用 `/api/worker/finish`，Worker Run 结束并释放模型。

## 模型生命周期：Worker Run 级复用

2026-08-15 联调发现：原代码虽然在模块级只创建了一个 `IDCreator`，但 Hivision 的 `extract_human_birefnet_lite` 和 RetinaFace handler 会根据 `RUN_MODE` 管理模块级 ONNX Runtime session。之前未进入 `beast` 模式，因此每个 Job 都重新加载 ONNX 模型。

现在 `api_server.py` 在 Worker Run 开始时：

```text
RUN_MODE=beast
```

模型仍然采用 lazy load：第一个 Job 第一次调用时加载 BiRefNet / RetinaFace。

之后同一个 Worker Run 的 Job 2、Job 3、... 复用已经存在的 ONNX Runtime session，不再重复加载。

Worker Run 结束时：

```text
1. 清空 hivision.creator.human_matting 的 ONNX session 引用
2. 清空 hivision.creator.face_detector.RETINAFCE_SESS
3. RUN_MODE 恢复为 normal
```

因此生命周期为：

```text
Worker Run 开始
    ↓
RUN_MODE=beast
    ↓
Job 1 → 第一次加载模型
    ↓
Job 2 → 复用模型
    ↓
Job 3 → 复用模型
    ↓
queue empty / worker error
    ↓
清理 ONNX sessions
    ↓
RUN_MODE=normal
```

这不是长期进程级模型常驻；模型只在当前 Worker Run 的处理窗口内保持。

### CPU 模式

本项目明确使用 CPU 推理，不切换 GPU，也不为了优化 GPU 增加依赖或配置。

BiRefNet + RetinaFace 本身内存占用较高，因此本方案重点是：

- 同一 Worker Run 内避免重复加载。
- Worker Run 结束主动释放模型引用。
- Lightning 空闲时不需要通过代码保持模型常驻。

## 每 Job 临时内存清理：2026-08-15

3 Job 测试中发现：虽然 ONNX model 已经成功在 Worker Run 内复用，但需要继续关注 Job 之间的 RSS 是否持续增长。

检查 `api_server.py` 后确认每 Job 会创建：

- `requests.Response`
- 输入图片 bytes
- PIL Image
- NumPy RGB/BGR 数组
- IDCreator 中间结果
- OpenCV 输出数组
- PNG encoded bytes
- `/next` 返回的 Job/Payload 对象
- heartbeat 线程和 Event

现在每个 Job 完成后增加显式清理：

```text
input_response.close()
↓
删除 input_data
↓
释放 PIL / NumPy / OpenCV / IDCreator 临时引用
↓
删除 output PNG bytes
↓
关闭 output Response
↓
gc.collect()
↓
Linux/glibc best-effort malloc_trim(0)
↓
记录当前 VmRSS
```

**注意：这里不会清理 BiRefNet / RetinaFace ONNX session。** 它们属于 Worker Run，必须保留到队列结束。

同时新增 Linux `/proc/self/status` 的 `VmRSS` 日志，例如：

```text
[QueueWorker] memory label=job_complete rss_mb=...
[QueueWorker] memory label=job_cleanup rss_mb=...
```

Worker Run 结束后也记录：

```text
[QueueWorker] memory label=worker_run_end rss_mb=...
```

这样可以区分：

1. Job 临时对象没有释放。
2. Python/glibc allocator 保留了已释放 heap。
3. ONNX Runtime session/arena 本身保留内存。
4. 真正存在跨 Job 的引用泄漏。

如果 `job_cleanup` 后 RSS 仍逐 Job 上升，需要结合这些日志进一步判断，而不能仅凭平台显示的“运行内存”直接认定是 Python 对象泄漏。

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
- 每个 Job 只启动一个 heartbeat 线程；CPU inference 本身仍由 `inference_lock` 严格串行。
- 输出使用 Job 返回的 R2 presigned PUT URL 写入 PNG。
- 单个 Job 失败不会停止整个 Worker Run；会回调 fail 后继续处理下一个 Job。
- Lightning Worker 会在调用 Vercel Bridge 前输出完整请求 URL，包括协议、主机名、端口和路径；不会输出 Worker Credential。
- 修复 `bridge_url + /api/worker/*` 重复拼接问题。
- 区分 Vercel Deployment Protection 401 与 Worker Credential 401。
- 修复前端 middleware 对 `/api/worker/*` 的浏览器 Cookie 认证拦截（由前端仓库完成）。
- 实测成功完成 3 Job 串行 Worker Run。
- 模型改为 Worker Run 级缓存：同一 Run 内 BiRefNet / RetinaFace 只加载一次，Run 结束主动清理。
- 每 Job 增加临时图片/HTTP/Python 对象清理、GC、Linux glibc best-effort trim，并记录 Worker RSS。

## 2026-08-15 联合调试结果

已验证完整链路：

```text
Vercel
 ↓
/process-queue
 ↓
Lightning Worker
 ↓
/api/worker/next
 ↓
claim Job
 ↓
R2 input
 ↓
CPU BiRefNet + RetinaFace
 ↓
R2 output
 ↓
/api/worker/complete
 ↓
next
 ↓
next
 ↓
finish
```

此前一次 3 Job 测试：

```text
Job 1 total=21759ms inference=20.410s
Job 2 total=22953ms inference=21.770s
Job 3 total=19940ms inference=19.006s
processed=3
```

日志中每个 Job 都出现 `Loading ONNX model took ...`，确认存在重复加载。

修复后测试：

```text
Job 1 inference=16.629s
Job 2 inference=15.682s
Job 3 inference=12.327s
processed=3
```

只有 Job 1 出现：

```text
Loading ONNX model took 2.5823 seconds
```

Job 2、Job 3：

```text
Loading ONNX model took 0.0000 seconds
```

并在 Run 结束看到：

```text
[QueueWorker] model cache cleared run=... scope=worker_run
```

确认 Worker Run 级模型复用已经生效。

下一阶段重点验证新增的 `job_complete` / `job_cleanup` RSS 日志，判断用户观察到的第 2、3 个 Job 内存上升是否来自真正的临时对象泄漏，还是 Python/glibc/ONNX Runtime allocator 的保留内存。

## 当前调试方案：Lightning Studio Linux 直接运行

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

Worker 打印：

```text
POST https://<preview-host>/api/worker/next
POST https://<preview-host>/api/worker/heartbeat
POST https://<preview-host>/api/worker/complete
POST https://<preview-host>/api/worker/fail
POST https://<preview-host>/api/worker/finish
```

不会打印 Credential。

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

1. 重启 Lightning Studio FastAPI，使最新 per-Job cleanup 代码生效。
2. 提交 3 个 Job。
3. 点击开始处理。
4. 检查每个 Job 的 `memory label=job_complete` 与 `memory label=job_cleanup`。
5. 如果 cleanup 后 RSS 基本稳定，说明之前的上升主要来自临时对象/allocator 保留。
6. 如果 cleanup 后 RSS 仍持续明显上升，需要继续定位 Hivision/ONNX Runtime 的跨 Job 内存缓存或真正的引用泄漏。
7. 确认 3 Job 均成功 complete。
8. 确认 finish 后日志出现 `model cache cleared` 和 `worker_run_end`。
9. 下一次新的 Worker Run 应重新加载模型一次；这是刻意设计的 Worker Run 生命周期，不是进程级常驻。
10. heartbeat、Worker 崩溃、lease recovery、重复 complete、fail-retry、credential expiry。
11. 调试链路稳定后，再切回 Lightning 平台 Wake 模式。

## 当前提交

后端最新 commit：`534b25f302ad48a5ece6966aded9a32296bdc337`。
