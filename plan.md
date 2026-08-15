# id-photo-back 开发计划

## 1. 架构目标

```text
Vercel
  ↓ POST /process-queue
Lightning Studio / Lightning Inference
  ↓
Single-use Worker Container
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
  └─ queue empty → finish → 释放模型 → 进程退出
                                      ↓
                                  Container stopped
                                      ↓
                                  scale-to-zero
```

Lightning 是无状态 Worker：**一个 Container 对应一次 Worker Run，Worker Run 结束后 Container 必须退出**。空闲时不保持专门的模型 Worker Run。模型生命周期绑定一次 Worker Run。

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
 ↓
记录 worker_run_end RSS
 ↓
退出 Worker 进程
```

这样可以避免模型在长期空闲进程中持续占用大量内存，并确保 Lightning Container 能真正停止并 scale-to-zero。

## 4. Worker Container 退出机制

此前 Worker thread 在 queue empty 后会调用 `/api/worker/finish` 并清理模型，但 Uvicorn 主进程仍然继续运行，因此 Lightning Container 不会因为 Worker Run 完成而停止。

现在 `_process_jobs()` 的 `finally` 在完成模型清理后调用：

```python
_shutdown_worker_process(worker_run_id, processed)
```

该函数使用：

```python
os._exit(0)
```

原因是 `_process_jobs()` 运行在后台 daemon thread 中；`raise SystemExit` 只会终止当前 thread，不会终止 Uvicorn 主进程。

因此完整生命周期为：

```text
queue empty
 ↓
POST /api/worker/finish
 ↓
clear ONNX sessions
 ↓
RUN_MODE=normal
 ↓
worker_run_end RSS log
 ↓
exiting process for scale-to-zero
 ↓
os._exit(0)
 ↓
Uvicorn process exits
 ↓
Docker Container stops
 ↓
Lightning scale-to-zero
```

Worker Run 发生未处理异常时同样经过 `_process_jobs()` 的 `finally`，Container 也会退出，避免出现“Worker 已停止但 Uvicorn 一直挂着”的空闲实例。

这是当前架构的硬性生命周期要求：**处理完 Queue 后不保留服务进程。**

## 5. 每 Job 临时内存生命周期

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

## 6. 预期性能变化

此前 3 Job 测试中每个 Job 都重新加载 BiRefNet：

```text
Job 1: Loading ONNX model ≈ 3.3s
Job 2: Loading ONNX model ≈ 2.6s
Job 3: Loading ONNX model ≈ 2.5s
```

模型加载累计约 8.4 秒。

修复后，同一个 Worker Run 中应该只有第一次实际调用产生模型加载日志。

新的 Worker Run 重新加载一次是预期行为，因为这是 Worker Run 级生命周期，而不是进程级常驻。

## 7. Queue Worker

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

## 8. Vercel Bridge

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

## 9. 动态 Preview Hostname

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

## 10. 错误分类

Vercel 返回 401 时区分：

```text
Protected deployment
    → vercel_deployment_protection

Unauthorized
    → worker_credential_rejected
```

避免把 Deployment Protection 错误误报成 Worker Credential 过期。

## 11. Production Docker

正式部署使用**单个 Docker Container**，不使用 `docker-compose.yml`。

启动入口：

```bash
uvicorn api_server:app --host 0.0.0.0 --port 8000
```

Docker Production 依赖分为：

```text
requirements.txt
    ↓
模型/推理依赖

requirements-worker.txt
    ↓
fastapi
uvicorn[standard]
python-multipart
pillow
gradio>=4.43.0
```

Docker 不安装 `requirements-app.txt`，但 **Gradio 必须保留为 Worker runtime dependency**，因为当前 `hivision` beauty plugin import 链仍然在模块导入阶段加载 Gradio。

### Gradio 依赖

Production Worker 导入 `hivision` 时会经过：

```text
hivision.creator
 ↓
hivision.plugin.beauty
 ↓
BeautyTools
 ├─ grind_skin.py → import gradio
 └─ whitening.py → import gradio
```

因此之前尝试从 `grind_skin.py` 移除 Gradio 后，Docker 又在 `whitening.py` 处启动失败：

```text
ModuleNotFoundError: No module named 'gradio'
```

最终采用稳定方案：**恢复 Gradio runtime dependency，而不启动 Gradio Web UI。**

`requirements-worker.txt` 必须包含：

```text
gradio>=4.43.0
```

Docker CMD 仍然只启动 FastAPI/Uvicorn。

### 模型文件

`.onnx` 模型文件直接存放在 Git 仓库中，必须进入 Docker image；`.dockerignore` 不排除 `*.onnx`。

### Docker 忽略规则

`.dockerignore` 排除 Git metadata、Python cache、虚拟环境、build artifacts、docs/demo、Markdown、日志、IDE/OS 文件和 `docker-compose.yml`，但保留 `.onnx`。

## 12. 当前调试方式

本地 Docker 验证：

```bash
docker build -t id-photo-back .
docker run --rm -p 8000:8000 id-photo-back
```

启动后检查：

```text
0.0.0.0:8000
```

当前 Docker 首次启动曾出现：

```text
ModuleNotFoundError: No module named 'gradio'
```

该问题最终通过把 Gradio 恢复为 Worker runtime dependency 解决，而不是继续修改 beauty plugin import 链。

另外可能看到 ONNX Runtime 的 GPU discovery warning；由于当前使用 CPU Execution Provider，该 warning 不属于启动失败原因。

## 13. 验证计划

### Docker 启动

1. 重新执行 Docker build。
2. 确认 `.onnx` 文件全部进入 image。
3. 确认容器启动时不再出现 `ModuleNotFoundError: gradio`。
4. 确认 FastAPI / Uvicorn 监听 `0.0.0.0:8000`。
5. 确认 `/process-queue` 可以正常接受 Lightning Platform 请求。

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

### Container scale-to-zero

完整 Worker Run 必须验证日志顺序：

```text
queue empty, worker finished
↓
model cache cleared
↓
worker_run_end
↓
exiting process for scale-to-zero
```

然后 Docker/Lightning Container 的主进程必须退出。

下一次点击“开始处理”时，应由 Vercel 再次唤醒新的 Lightning Container，并重新创建新的 Worker Run；新 Worker Run 首次推理重新加载一次模型是预期行为。

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
 ↓
clear model cache
 ↓
process exit
 ↓
Container stopped / scale-to-zero
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

## 14. 正式生产前

1. 保持模型为 CPU Execution Provider。
2. 根据真实内存峰值评估 Lightning Instance 内存规格。
3. 根据真实 Job 数量调整 Worker Run 最大处理时长。
4. 验证 Worker Run 结束后模型引用确实释放。
5. 验证每 Job 临时内存不会无限增长。
6. 验证 Production Docker image 能启动并运行 `/process-queue`。
7. 验证 queue empty 后 Container 进程实际退出并 scale-to-zero。
8. 再切换到 Lightning Platform serverless / inference API。
