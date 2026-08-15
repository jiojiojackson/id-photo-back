# id-photo-back 开发计划

## 1. 当前架构目标

```text
Vercel
  ↓ POST /process-queue
Lightning Deployment
  ↓
FastAPI / Uvicorn 主进程
  ↓
后台 Queue Worker thread
  ↓
next → claim → R2 input → CPU inference → R2 output
  ↓
complete/fail → next
  ↓
queue empty → finish
  ↓
释放 Worker Run 模型缓存
  ↓
Worker thread 停止
  ↓
Uvicorn 保持运行
  ↓
Lightning Autoscaler / Idle timeout
  ↓
replica 1 → 0
```

**核心原则：Worker Run 生命周期与 Lightning Deployment 主进程生命周期必须分离。**

一个 Worker Run 完成后不代表 Uvicorn/FastAPI 服务应该退出。Deployment 的 replica 生命周期由 Lightning 平台管理。

## 2. Lightning Deployment 当前配置

已确认当前 Deployment：

```text
Minimum replicas: 0
Maximum replicas: 1
Autoscaling metric: CPU >= 95
Scale up cooldown: 0s
Scale down cooldown: 0s
Idle timeout: 300s
Machine: 4 x Default (CPU)
Port: 8000
Readiness health check: none
```

因此目标行为是：

```text
有 Queue 工作
    ↓
replica = 1
    ↓
CPU 推理
    ↓
Queue empty
    ↓
Worker thread 停止
    ↓
CPU/workload 降低
    ↓
Idle timeout 300s
    ↓
Lightning Autoscaler
    ↓
replica = 0
```

## 3. 不允许应用主动退出 Deployment

此前为了让 Container 停止，加入过：

```python
os._exit(0)
```

结果实测为：

```text
Worker Run 完成
 ↓
os._exit(0)
 ↓
旧 replica 消失
 ↓
Lightning 创建新 replica
 ↓
新 replica 只有 Docker/Uvicorn，没有 Worker thread
 ↓
新 replica 长时间保持运行
```

因此不能通过结束 Python/Uvicorn 主进程实现 scale-to-zero。

已经撤销 `_shutdown_worker_process()` 以及 `os._exit(0)`。

当前 `_process_jobs()` 的 `finally` 只负责：

```text
_finish_worker_models()
 ↓
worker_running = False
 ↓
worker stopped log
```

**Uvicorn 主进程继续运行。**

## 4. Worker Run 模型生命周期

### 开始

```text
_process_jobs()
 ↓
_prepare_worker_models()
 ↓
RUN_MODE=beast
```

模型 lazy load。

### Job 1

第一次推理加载 BiRefNet / RetinaFace ONNX session。

### Job 2..N

复用同一个 Worker Run 的 ONNX session，不重新加载。

### Worker Run 结束

```text
_clear_worker_model_cache()
 ↓
RUN_MODE=normal
 ↓
gc.collect()
 ↓
worker_run_end RSS
 ↓
Worker thread stopped
```

不退出 Uvicorn。

## 5. 每 Job 内存生命周期

每 Job：

```text
R2 response
 ↓
input bytes
 ↓
PIL / NumPy / OpenCV
 ↓
IDCreator result
 ↓
PNG bytes
 ↓
R2 upload
 ↓
release references
 ↓
gc.collect()
 ↓
malloc_trim(0) best effort
```

记录：

```text
job_complete RSS
job_cleanup RSS
worker_run_end RSS
```

ONNX session 不在 Job 之间释放，只在 Worker Run 结束时释放。

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

保留 `inference_lock`。

Worker Run 结束后：

```text
worker_running = false
```

不会启动任何新的后台 Worker。

## 7. Worker Bridge

保持：

```text
POST /api/worker/next
POST /api/worker/heartbeat
POST /api/worker/complete
POST /api/worker/fail
POST /api/worker/finish
```

Worker 使用短期 credential：

```http
Authorization: Bearer <short-lived-worker-credential>
```

不使用 Lightning API Key。

## 8. 动态 Vercel Host

`vercel_origin` 是当前 Vercel 请求对应的实际 Preview/Production host。

Worker Bridge base URL：

```text
https://<vercel-origin>/api/worker
```

然后追加：

```text
/next
/heartbeat
/complete
/fail
/finish
```

禁止硬编码 Vercel hostname。

## 9. Production Docker

使用单个 Dockerfile，不使用 `docker-compose.yml`。

启动：

```bash
uvicorn api_server:app --host 0.0.0.0 --port 8000
```

依赖：

```text
requirements.txt
requirements-worker.txt
```

Worker runtime：

```text
fastapi
uvicorn[standard]
python-multipart
pillow
gradio>=4.43.0
```

Gradio 是现有 beauty plugin import chain 的 runtime dependency，但不会启动 Gradio UI。

`.onnx` 模型文件直接包含在 Docker image；`.dockerignore` 不排除 `.onnx`。

## 10. CPU 推理

当前明确使用 CPU Execution Provider。

不增加 GPU 专用依赖。

## 11. 错误处理

Worker 401 分类：

```text
Vercel Deployment Protection
    → vercel_deployment_protection

Worker Credential invalid/expired
    → worker_credential_rejected
```

单 Job fail 通过 `/api/worker/fail` 回报，由 Vercel 根据 retry policy 决定重试或最终失败。

Worker Run 本身异常时仍清理模型并结束 Worker thread；不主动结束 Uvicorn。

## 12. Scale-to-zero 验证计划

### 第一次测试

1. 发布撤销 `os._exit(0)` 后的新 Docker image。
2. Lightning Deployment 保持：

```text
min replicas = 0
max replicas = 1
CPU >= 95
idle timeout = 300s
```

3. 创建 3 个 Job。
4. 观察 Worker 日志应该结束于：

```text
queue empty, worker finished processed=3
model cache cleared
stopped run=... processed=3
```

不应该再出现：

```text
exiting process for scale-to-zero
```

### 第二次测试

Worker thread 停止后观察主进程：

```text
Uvicorn 仍监听 8000
```

同时 CPU/workload 降低。

### 第三次测试

等待至少 `Idle timeout = 300s`，观察 Lightning：

```text
running (1/1)
 ↓
0 replicas / stopped
```

### 如果仍然不能 scale-to-zero

不要再修改 Python 退出逻辑。

重点调查 Lightning Deployment 本身：

1. Idle timeout 的 activity 判定。
2. CPU metric 的 scale-down 行为。
3. Deployment proxy 是否存在持续活动连接。
4. 是否存在内部 health/traffic 请求。
5. Readiness health check 是否需要配置。
6. Release 状态是否影响 scale-down。

## 13. 性能与内存验证

同一 Worker Run：

```text
Job 1 → 首次模型加载
Job 2 → reuse
Job 3 → reuse
```

新的 replica / Worker Run：

```text
第一次 Job → 重新加载模型
```

这是预期行为。

Job cleanup RSS 不要求立即回到启动时水平；glibc 和 ONNX Runtime 可能保留可复用内存。真正重要的是 Worker Run 结束后模型 session 被清理，以及不会因为 Job 数量无限增长。

## 14. 当前生产前检查清单

- [x] Worker Bridge
- [x] heartbeat
- [x] lease
- [x] complete/fail/finish
- [x] 动态 Vercel Preview hostname
- [x] CPU inference
- [x] ONNX model cache per Worker Run
- [x] per Job memory cleanup
- [x] Production Docker 单容器
- [x] `.dockerignore` 保留 `.onnx`
- [x] Gradio runtime dependency
- [x] Lightning `min replicas=0`
- [x] Lightning `max replicas=1`
- [x] Lightning `idle timeout=300s`
- [x] 撤销应用主动 `os._exit(0)`
- [ ] 实测 Worker thread 停止后 Lightning 自动 scale-to-zero
- [ ] 实测 300s idle timeout 后 replica=0
- [ ] 实测 scale-to-zero 后下一次 `/process-queue` 能重新创建 replica
- [ ] 重新验证 heartbeat / lease recovery / fail-retry / duplicate complete / credential expiration
