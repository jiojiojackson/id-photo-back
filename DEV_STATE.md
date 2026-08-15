# DEV_STATE

当前开发分支：`agent/queue-worker-bridge`

本仓库是证件照 CPU 推理 Worker，对应前端 `id-photo-front` 的 Vercel + Neon + R2 + Vercel Queue + Lightning 架构。

## 当前状态

后端业务链路已经调试完成，现在进入 **Production Docker Container 打包阶段**。

本阶段只做 Docker packaging / dependency cleanup，不改变已经验证的 Worker Bridge、heartbeat、lease、inference 或 Worker Run 生命周期逻辑。

Lightning 使用单个 Docker Container，启动：

```bash
uvicorn api_server:app --host 0.0.0.0 --port 8000
```

不使用 `docker-compose.yml`。

## Worker 架构

```text
Vercel
  ↓ POST /process-queue
Lightning Container
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

Lightning Container 是无状态 Worker。模型生命周期绑定一次 Worker Run，空闲时不保持专门的模型 Worker Run。

## Docker Production 配置

当前 Dockerfile：

```text
python:3.10-slim
↓
系统依赖：ffmpeg / libgl1 / libglib2.0-0
↓
requirements.txt
requirements-worker.txt
↓
复制整个应用（模型 .onnx 文件保留）
↓
uvicorn api_server:app :8000
```

### `.dockerignore`

已经新增 `.dockerignore`，排除：

- `.git` / `.github`
- Python cache / `.pyc`
- 虚拟环境
- build / dist / egg-info
- 日志和临时文件
- docs / demo / Markdown 文档
- docker-compose.yml
- Dockerfile 本身
- IDE / OS 文件

**不会排除 `.onnx`。** 模型文件直接存放在 Git 仓库中，并必须进入 Production Docker image。

## Production dependencies

模型/推理依赖继续使用：

```text
requirements.txt
```

内容包括 OpenCV、ONNX Runtime、NumPy、requests、mtcnn-runtime 等。

新增：

```text
requirements-worker.txt
```

只负责 Production Worker Web/API 依赖：

```text
fastapi
uvicorn[standard]
python-multipart
pillow
```

Docker 不再安装 `requirements-app.txt`，因此不会为了 Worker 引入 Gradio。

原 Dockerfile 中：

```text
pip install -r requirements.txt -r requirements-app.txt
pip install "fastapi[standard]" python-multipart pillow
```

存在重复安装和不必要的 UI 依赖，现已改为：

```text
pip install -r requirements.txt -r requirements-worker.txt
```

## Docker 启动方式

正式单容器启动：

```bash
docker build -t id-photo-back .
docker run --rm -p 8000:8000 id-photo-back
```

容器监听：

```text
0.0.0.0:8000
```

入口：

```text
api_server:app
```

`docker-compose.yml` 不参与 Production，不需要修改。

## 环境变量边界

Backend Docker **不配置**：

```text
LIGHTNING_API_KEY
LIGHTNING_API_URL
DATABASE_URL
R2_*
VERCEL_QUEUE_*
```

Vercel → Lightning 使用 `LIGHTNING_API_KEY` 唤醒 Lightning；Lightning → Vercel Bridge 使用 wake payload 中的短期 Worker Credential。

Worker Container 本身是无状态 inference worker。

## 模型生命周期

当前 `api_server.py` 使用 `RUN_MODE=beast` 管理 Worker Run 级模型缓存。

```text
Worker Run 开始
    ↓
RUN_MODE=beast
    ↓
Job 1 → 第一次加载 BiRefNet / RetinaFace
    ↓
Job 2 → 复用
    ↓
Job 3 → 复用
    ↓
queue empty / worker error
    ↓
清理 ONNX sessions
    ↓
RUN_MODE=normal
```

Dockerfile 默认：

```text
RUN_MODE=beast
```

模型文件 `.onnx` 直接包含在 Docker image 中，不在容器启动时从网络下载。

## CPU 推理

本项目明确使用 CPU 推理，不增加 GPU 专用依赖或配置。

## 每 Job 临时内存清理

每 Job 完成后继续执行：

```text
Response.close()
↓
释放 input bytes / PIL / NumPy / OpenCV / IDCreator 临时引用
↓
释放 output bytes
↓
gc.collect()
↓
Linux/glibc best-effort malloc_trim(0)
↓
记录 VmRSS
```

不会在 Job 之间清理 BiRefNet / RetinaFace ONNX session，因为它们属于 Worker Run。

## Worker Bridge

已完成并保持不变：

```text
POST /api/worker/next
POST /api/worker/heartbeat
POST /api/worker/complete
POST /api/worker/fail
POST /api/worker/finish
```

统一使用：

```http
Authorization: Bearer <short-lived-worker-credential>
```

Wake payload 使用：

```text
worker_run_id
bridge_url
vercel_origin
worker_credential
worker_credential_expires_at
```

不保存 Worker Credential，不读取 Lightning API Key。

## Worker Run 参数

```text
lease = 10 分钟
heartbeat = 60 秒
presigned URL = 15 分钟
MAX_ATTEMPTS = 5
Worker stale = 120 秒
```

Worker Run 内 Job 严格串行。

单个 Job fail 后由 Vercel 根据 `MAX_ATTEMPTS=5` 决定重新排队或最终失败；单个 Job 失败不会停止整个 Worker Run。

## 已验证链路

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
finish
```

已实测 3 Job 串行 Worker Run 成功，并确认同一 Worker Run 内模型只实际加载一次。

## Docker 化本阶段修改

已完成：

1. 新增 `.dockerignore`。
2. 保留所有 `.onnx` 模型文件进入 image。
3. Dockerfile 去除重复 FastAPI / multipart / pillow 安装。
4. 新增 `requirements-worker.txt`，将 Production Worker Web/API 依赖与模型依赖分开。
5. Dockerfile 不再安装 `requirements-app.txt`，避免引入 Gradio。
6. 保持单容器 `uvicorn api_server:app --host 0.0.0.0 --port 8000`。
7. 不修改 `docker-compose.yml`，因为 Production 不使用它。
8. 不修改 Worker Bridge 和推理业务逻辑。

## 当前待验证

1. 在本地/CI 执行 Docker build。
2. 确认 `.onnx` 文件全部进入 image。
3. 确认容器可以启动 FastAPI / Uvicorn。
4. 确认 `/process-queue` 可以被 Lightning Platform 正常调用。
5. 重新执行完整 3 Job Worker Run。
6. 检查 Job cleanup / Worker Run end RSS 日志。
7. 验证 heartbeat、lease recovery、fail/retry、duplicate complete、credential expiration。
8. 验证新 Worker Run 会重新加载模型一次，这是预期行为。

## 后端当前提交基线

进入 Docker packaging 前的业务基线：`534b25f302ad48a5ece6966aded9a32296bdc337`。

Docker packaging commits：

```text
44135e2e828be6b01be495d1a08971ea57c0352b  # .dockerignore
cdd75d15ddff6caddea687a836f4bfc932c7b0aa  # requirements-worker.txt
df5818a81fcb590c2acc156c180c985e48bca7f5  # Dockerfile
```
