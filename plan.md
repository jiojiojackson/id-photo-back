# id-photo-back 开发计划

## 1. 架构目标

```text
Vercel
  ↓ POST /process-queue
Lightning Studio / Lightning Inference
  ↓
Worker Run
  ├─ 初始化 Worker Run 模型缓存
  ├─ POST /api/worker/next
  ├─ claim Job
  ├─ R2 input GET
  ├─ CPU inference
  ├─ R2 output PUT
  ├─ POST /api/worker/complete
  ├─ heartbeat
  └─ queue empty → finish → 释放模型
```

Lightning 是无状态 Worker：空闲时不保持一个专门的模型 Worker Run。模型生命周期绑定一次 Worker Run。

## 2. CPU 推理原则

当前明确使用 CPU Execution Provider。

不进行 GPU 优化，不添加 GPU 专用依赖。

原因：BiRefNet + RetinaFace 内存占用较高，当前部署目标优先控制 Lightning 推理 API 的运行内存和成本。

## 3. Worker Run 模型生命周期

HivisionIDPhotos 的 BiRefNet 和 RetinaFace handler 使用模块级 ONNX Runtime session，并通过 `RUN_MODE=beast` 控制是否在连续调用之间保留 session。

后端现在将这个机制封装为 Worker Run 生命周期：

### Worker Run 开始

```text
_process_jobs()
 ↓
_prepare_worker_models()
 ↓
RUN_MODE=beast
```

不主动加载模型，保持 lazy load。

### 第一个 Job

第一次调用 `IDCreator`：

```text
BiRefNet session → load
RetinaFace session → load
```

### 后续 Job

```text
Job 2 → reuse
Job 3 → reuse
Job N → reuse
```

不能在每个 Job 前重新设置非 beast 模式。

### Worker Run 结束

无论 queue empty 还是 Worker 异常：

```text
_clear_worker_model_cache()
 ↓
BiRefNet session = None
RetinaFace session = None
 ↓
RUN_MODE=normal
```

这样可以避免模型在长期空闲进程中持续占用大量内存。

## 4. 每 Job 临时内存生命周期

模型复用解决的是 ONNX session 的重复加载，但 Job 之间还有大量短生命周期对象：

```text
R2 response
 ↓
input bytes
 ↓
PIL Image
 ↓
NumPy RGB/BGR
 ↓
IDCreator result / OpenCV output
 ↓
PNG encoded bytes
 ↓
R2 upload response
```

这些对象不能跨 Job 保留。

后端现在在每 Job 的推理、上传、callback 完成后显式：

```text
Response.close()
del input_data / output
job = None
payload = None
heartbeat thread/event = None
gc.collect()
libc.malloc_trim(0)  # Linux/glibc best effort
```

同时 `_run_inference()` 在 `finally` 中释放 PIL / NumPy / OpenCV / IDCreator 临时引用。

**不在这里释放 ONNX session**，因为 BiRefNet / RetinaFace 属于 Worker Run 缓存。

### RSS 诊断

不新增 `psutil` 依赖，直接读取 Linux：

```text
/proc/self/status → VmRSS
```

记录：

```text
memory label=job_complete rss_mb=...
memory label=job_cleanup rss_mb=...
memory label=worker_run_end rss_mb=...
```

目的不是假设一定存在内存泄漏，而是区分：

1. Python 临时对象未释放。
2. CPython/glibc allocator 保留已释放 heap。
3. ONNX Runtime arena/session 保留内存。
4. 真正的跨 Job 引用泄漏。

## 5. 预期性能变化

此前 3 Job 测试中每个 Job 都重新加载 BiRefNet：

```text
Job 1: Loading ONNX model ≈ 3.3s
Job 2: Loading ONNX model ≈ 2.6s
Job 3: Loading ONNX model ≈ 2.5s
```

模型加载累计约 8.4 秒。

修复后，同一个 Worker Run 中应该只有第一次实际调用产生模型加载日志。

新的 Worker Run 重新加载一次是预期行为，因为这是 Worker Run 级生命周期，而不是进程级常驻。

## 6. Queue Worker

Worker 严格串行：

```text
next
 ↓
Job
 ↓
inference
 ↓
complete/fail
 ↓
next
```

`inference_lock` 继续保留，避免同步 `/generate` 与 Queue Worker 同时执行模型推理。

## 7. Vercel Bridge

```text
POST /api/worker/next
POST /api/worker/heartbeat
POST /api/worker/complete
POST /api/worker/fail
POST /api/worker/finish
```

Worker 使用：

```http
Authorization: Bearer <short-lived-worker-credential>
```

不使用 Lightning API Key 访问 Vercel Bridge。

## 8. 动态 Preview Hostname

禁止硬编码 Vercel hostname。

`/process-queue` 使用 wake payload 中的 `vercel_origin` 构造：

```text
https://<current-preview-host>/api/worker
```

然后：

```text
/next
/heartbeat
/complete
/fail
/finish
```

分别追加到 `/api/worker` 基础 URL。

## 9. 错误分类

Vercel 返回 401 时区分：

```text
Protected deployment
    → vercel_deployment_protection

Unauthorized
    → worker_credential_rejected
```

避免把 Deployment Protection 错误误报成 Worker Credential 过期。

## 10. 当前调试方式

当前运行在 Lightning Studio Linux，不使用 Docker：

```bash
cd /path/to/id-photo-back
python3 -m uvicorn api_server:app --host 0.0.0.0 --port 8000
```

Debug 模式不需要 Lightning API Key。

## 11. 验证计划

### 模型缓存

提交 3 个 Job：

1. 首个 Job 应出现一次 `Loading ONNX model took ...`。
2. Job 2 不应重新加载 BiRefNet。
3. Job 3 不应重新加载 BiRefNet。
4. RetinaFace 也应复用同一 session。
5. finish 后应出现 `model cache cleared`。
6. 下一次新的 Worker Run 应重新加载一次。

### 每 Job 内存

提交至少 3～5 个 Job，观察：

```text
memory label=job_complete rss_mb=...
memory label=job_cleanup rss_mb=...
```

重点比较 Job 1 → Job 2 → Job 3 的 `job_cleanup` RSS。

- 如果 `job_cleanup` 后基本稳定：临时对象已被清理。
- 如果 `job_cleanup` 后仍逐 Job 增长：继续检查 ONNX Runtime arena、Hivision 内部缓存以及真正的引用泄漏。
- 不应因为 RSS 没有立刻下降就直接判断为泄漏；glibc/ONNX Runtime 可能保留可复用内存。

### 完整链路

```text
next → claim
 ↓
R2 input
 ↓
CPU inference
 ↓
R2 output
 ↓
complete
 ↓
next
 ↓
finish
```

### 后续

- heartbeat
- lease recovery
- Worker crash recovery
- fail/retry
- duplicate complete idempotency
- credential expiration
- 多 Worker Run 并发保护
- Lightning Platform Wake 模式

## 12. 正式生产前

1. 保持模型为 CPU Execution Provider。
2. 根据真实内存峰值评估 Lightning Instance 内存规格。
3. 根据真实 Job 数量调整 Worker Run 最大处理时长。
4. 验证 Worker Run 结束后模型引用确实释放。
5. 验证每 Job 临时内存不会无限增长。
6. 再切换到 Lightning Platform serverless / inference API。
