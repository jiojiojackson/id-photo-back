# DEV_STATE

当前开发分支：`agent/queue-worker-bridge`

本仓库是证件照 CPU 推理 Worker，对应前端 `id-photo-front` 的 Vercel + Neon + R2 + Vercel Queue + Lightning 架构。

## 当前状态

后端核心业务链路已经调试完成，当前 Production Docker / Lightning Deployment 重点是：

- 单 Docker Container。
- CPU 推理。
- `.onnx` 模型直接包含在 image。
- Worker Bridge、heartbeat、lease、complete/fail/finish 已完成。
- 同一 Worker Run 内复用 BiRefNet / RetinaFace ONNX session。
- Worker Run 结束时释放模型缓存和每 Job 临时内存。
- **Worker Run 结束不能主动退出 Uvicorn 主进程。** Lightning Deployment 的 replica 生命周期由 Lightning Autoscaler 管理。
- Production ASGI entrypoint 为 `app_entry.py`，在导入 `api_server.app` 后提供平台所需的 `/` 与 `/health` 元数据端点。

## System Metadata / Health Endpoints

Docker 当前启动：

```text
uvicorn app_entry:app --host 0.0.0.0 --port 8000
```

`app_entry.py` 不改变现有业务逻辑，只给现有 FastAPI app 增加两个轻量端点：

```text
GET /
GET /health
```

`GET /` 返回：

```text
service
version
status
worker_running
endpoints
```

`GET /health` 返回：

```text
status=healthy
service
version
worker_running
started_at
checked_at
```

两个端点都不会：

- 启动 Queue Worker。
- 访问 Vercel Bridge。
- 访问 Queue。
- 加载 ONNX 模型。
- 修改 Job 状态。

因此它们适合 Lightning / Docker / 反向代理进行轻量 liveness/metadata 检查，不会因为健康检查而唤醒业务 Worker。

## Lightning Deployment 当前配置

当前已确认 Deployment：

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

因此 scale-to-zero 应由 Lightning 平台在 replica 空闲达到 Idle timeout 后完成，而不是由 Python/Uvicorn 进程自行退出。

## 正确的进程生命周期

```text
Lightning Deployment
  ↓
Uvicorn / FastAPI 主进程保持运行
  ↓
GET /health 可被平台检查，不启动 Worker
  ↓
POST /process-queue
  ↓
启动后台 Queue Worker thread
  ↓
next → claim → R2 input → CPU inference → R2 output
  ↓
complete/fail → next
  ↓
queue empty → finish
  ↓
清理 Worker Run 模型缓存
  ↓
Worker thread 停止
  ↓
Uvicorn 继续监听
  ↓
CPU / workload 降低
  ↓
Lightning Idle timeout 300s
  ↓
Lightning Autoscaler 将 replica 从 1 缩到 0
```

### 重要：禁止应用主动退出

此前曾加入：

```python
os._exit(0)
```

尝试通过进程退出实现 scale-to-zero。

实测发现旧 replica 退出后 Lightning 会重新创建一个新 replica；新 replica 只有 Docker/Uvicorn，没有 Worker thread，却持续保持 `1/1`。这说明应用主动结束 Deployment 主进程不是正确的 scale-to-zero 触发方式，并可能被 Deployment controller 当作需要补足 replica 的进程终止。

因此当前已撤销 `os._exit(0)`。

现在 `_process_jobs()` 的 `finally` 只做：

```text
_finish_worker_models()
↓
worker_running = False
↓
打印 Worker stopped
```

**不会结束 Uvicorn 主进程。**

## Worker Run 模型生命周期

HivisionIDPhotos 的 BiRefNet 和 RetinaFace handler 使用模块级 ONNX Runtime session，并通过 `RUN_MODE=beast` 控制连续调用之间的 session 复用。

### Worker Run 开始

```text
_process_jobs()
 ↓
_prepare_worker_models()
 ↓
RUN_MODE=beast
```

模型保持 lazy load。

### 第一个 Job

首次调用 `IDCreator` 时加载 BiRefNet / RetinaFace。

### 后续 Job

同一个 Worker Run 内复用已经加载的 ONNX session，不重复加载。

### Worker Run 结束

无论 queue empty 还是 Worker error：

```text
_clear_worker_model_cache()
 ↓
RUN_MODE=normal
 ↓
gc.collect()
 ↓
记录 worker_run_end RSS
 ↓
Worker thread stopped
```

模型不会在每个 Job 之间释放。

## 内存清理

每 Job 完成后继续清理：

```text
Response.close()
↓
释放 input/output bytes
↓
释放 PIL / NumPy / OpenCV / result 引用
↓
gc.collect()
↓
Linux/glibc best-effort malloc_trim(0)
↓
记录 VmRSS
```

日志包括：

```text
memory label=job_complete rss_mb=...
memory label=job_cleanup rss_mb=...
memory label=worker_run_end rss_mb=...
```

之前实测 Worker Run 结束时 RSS 可从约 12.8 GB 降到约 318 MB，说明模型缓存清理正常。

## Queue Worker

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

`inference_lock` 保留，避免同步 `/generate` 与 Queue Worker 同时执行推理。

Worker Run 空闲时不启动新的 Worker thread；`worker_running` 只在 `/process-queue` 成功启动 Worker 后为 true，Worker Run 结束后恢复 false。

## Worker Bridge

保持现有接口：

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

Wake payload 包含：

```text
worker_run_id
bridge_url
vercel_origin
worker_credential
worker_credential_expires_at
```

Worker Credential 不持久化。

## 动态 Vercel Host

禁止硬编码 Vercel hostname。

`vercel_origin` 是当前收到 `/api/jobs/start` 的实际 Preview/Production host；Worker 根据它构造：

```text
https://<current-host>/api/worker
```

然后访问：

```text
/next
/heartbeat
/complete
/fail
/finish
```

## 错误分类

Vercel 401 区分：

```text
Vercel Deployment Protection
    → vercel_deployment_protection

Worker Credential 无效/过期
    → worker_credential_rejected
```

## Docker Production

正式部署使用单个 Docker Container，不使用 `docker-compose.yml`。

启动：

```bash
uvicorn app_entry:app --host 0.0.0.0 --port 8000
```

`app_entry.py` 是很薄的 ASGI 包装层，业务实现仍在 `api_server.py`。

Docker 基础依赖：

```text
python:3.10-slim
ffmpeg
libgl1
libglib2.0-0
requirements.txt
requirements-worker.txt
```

`requirements-worker.txt` 包含：

```text
fastapi
uvicorn[standard]
python-multipart
pillow
gradio>=4.43.0
```

Gradio 必须保留，因为当前 `hivision` beauty plugin import 链在导入 `IDCreator` 时会加载：

```text
beauty.grind_skin → import gradio
beauty.whitening → import gradio
```

Production 不启动 Gradio UI。

## 模型文件

`.onnx` 模型直接存放于 Git repository，并必须进入 Docker image。

`.dockerignore` **不能排除 `*.onnx`**。

## 环境变量边界

Backend Docker 不配置：

```text
LIGHTNING_API_KEY
LIGHTNING_API_URL
DATABASE_URL
R2_*
VERCEL_QUEUE_*
```

Vercel 使用 `LIGHTNING_API_KEY` 唤醒 Lightning；Worker → Vercel Bridge 使用 wake payload 中的短期 Worker Credential。

## 已验证链路

```text
Vercel /process-queue
 ↓
Lightning replica
 ↓
Worker thread
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
finish
 ↓
clear model cache
 ↓
Worker thread stopped
 ↓
Uvicorn remains alive
 ↓
Lightning autoscaler / idle timeout
 ↓
replicas → 0
```

健康检查链路现在为：

```text
Lightning / platform
 ↓
GET /health
 ↓
app_entry.py
 ↓
200 JSON metadata
```

不会进入 Worker Bridge 或 Queue。

## 最近修复

### Docker / Gradio

Docker 启动曾因：

```text
ModuleNotFoundError: No module named 'gradio'
```

失败。最终通过把 `gradio>=4.43.0` 恢复为 Worker runtime dependency 解决，没有继续修改 beauty plugin import 链。

### Worker 进程退出

曾加入 `os._exit(0)`，日志可以显示：

```text
exiting process for scale-to-zero
```

但 Lightning 随后重新创建空闲 replica，因此该方案已撤销。

当前正确策略是：**应用只结束 Worker thread，不结束 Deployment 主进程；由 Lightning Autoscaler 根据 CPU / Idle timeout 管理 replica 生命周期。**

### Root / Health 404

Production 原先直接使用 `api_server:app`，没有定义 `/` 和 `/health`，因此平台访问这些端点得到 404。

当前增加 `app_entry.py`：

```text
GET /
GET /health
```

两个端点只返回服务元数据，不启动 Worker、不访问 Queue、不访问 Vercel Bridge、不加载模型。

Docker CMD 已改为：

```text
uvicorn app_entry:app --host 0.0.0.0 --port 8000
```

这样不会改变既有 `/generate` 与 `/process-queue` 行为，同时为平台提供稳定的 200 health/metadata response。

## 当前待验证

1. 构建包含 `app_entry.py` 的 Docker image。
2. 本地运行 Container 后确认：

```text
GET /       → 200 JSON
GET /health → 200 JSON
```

3. 确认 `/health` 不启动 Worker、不加载模型。
4. 完成一次 3 Job Worker Run。
5. 确认日志最后为：

```text
queue empty, worker finished processed=3
model cache cleared
stopped run=... processed=3
```

且不再出现：

```text
exiting process for scale-to-zero
```

6. 确认 Worker thread 停止后 Uvicorn 主进程继续运行。
7. 确认 CPU/workload 降低后，Lightning `Idle timeout=300s` 生效。
8. 确认 replica 从 `1/1` 自动变为 `0`，而不是重新创建一个空闲 replica。
9. 如果 300 秒后仍不能 scale-to-zero，再单独调查 Lightning Deployment autoscaler 的 activity/idle 判定，不再通过应用主动退出进程解决。
10. 下一次用户开始处理时，确认 Lightning 能重新创建 replica，并启动新的 Worker Run。

## 当前关键提交

本次修复新增：

```text
app_entry.py
```

并将 Docker production ASGI entrypoint 从：

```text
api_server:app
```

改为：

```text
app_entry:app
```

Worker Run 与 Deployment 主进程的生命周期现在明确分离，同时 Deployment 拥有稳定的 root/health metadata endpoints。