# DEV_STATE

当前开发分支：`agent/queue-worker-bridge`

本仓库是证件照 CPU 推理 Worker，对应前端 `id-photo-front` 的 Vercel + Neon + R2 + Vercel Queue + Lightning 架构。

## 当前状态

后端业务链路已经调试完成，现在进入 **Production Docker Container 打包阶段**。

本阶段只做 Docker packaging / dependency cleanup 和单次 Worker Container 生命周期处理，不改变已经验证的 Worker Bridge、heartbeat、lease、inference 或 Worker Run 业务逻辑。

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
  └─ queue empty → finish → 释放模型 → 进程退出
                                  ↓
                              Container stopped
                                  ↓
                              scale-to-zero
```

Lightning Container 是**单次 Worker Run、无状态、处理完即退出**的 Worker。模型生命周期绑定一次 Worker Run。

## Worker Container 生命周期

这是本次重要修复。

此前 `_process_jobs()` 在 queue empty 后只执行：

```text
finish
↓
_clear_worker_model_cache()
↓
RUN_MODE=normal
↓
worker_running=False
```

但是 Uvicorn 主进程仍然继续运行，因此即使 Worker Run 已经完成，Lightning Container 仍然处于运行状态，无法因为进程结束而 scale-to-zero。

现在 `_process_jobs()` 的 `finally` 在完成模型清理和日志记录后调用：

```python
_shutdown_worker_process(worker_run_id, processed)
```

该函数使用：

```python
os._exit(0)
```

原因是 `_process_jobs()` 本身运行在后台 daemon thread 中；如果只使用 `raise SystemExit`，只会退出当前 Worker thread，Uvicorn 主进程仍会存活。

当前生命周期：

```text
POST /process-queue
      ↓
启动 Worker thread
      ↓
处理 Queue Jobs
      ↓
queue empty
      ↓
POST /api/worker/finish
      ↓
清理 ONNX sessions
      ↓
RUN_MODE=normal
      ↓
记录 worker_run_end RSS
      ↓
输出 exiting process for scale-to-zero
      ↓
os._exit(0)
      ↓
Uvicorn 进程结束
      ↓
Docker Container stopped
      ↓
Lightning scale-to-zero
```

如果 Worker Run 因异常停止，也会经过同一个 `finally`，因此 Container 同样会退出，不会留下一个空闲的 Uvicorn 服务。

这是符合当前架构的：**一个 Container 对应一次 Worker Run，Run 完成后 Container 必须结束。**

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

负责 Production Worker Web/API 以及现有 beauty plugin 的运行时依赖：

```text
fastapi
uvicorn[standard]
python-multipart
pillow
gradio>=4.43.0
```

**Gradio 需要保留。** 虽然 Production Worker 不启动 Gradio UI，但 `hivision` 的 beauty plugin import 链会在导入 `IDCreator` 时加载 `grind_skin.py` 和 `whitening.py`，而这两个模块当前仍然包含 `import gradio as gr`。因此为了保证 Production Worker 能正常启动，Docker 镜像必须安装 Gradio。

Docker 不再安装 `requirements-app.txt`，但 `requirements-worker.txt` 会显式提供 Worker 实际 import 所需的 Gradio runtime dependency。

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

## Docker 首次启动问题与修复

第一次使用新的 Production Docker Image 启动时，容器在导入 `api_server.py` 阶段失败：

```text
ModuleNotFoundError: No module named 'gradio'
```

第一次定位时发现 `grind_skin.py` 包含旧 Gradio Demo/UI import。尝试从该模块删除 Gradio 后，重新构建镜像又在：

```text
hivision.plugin.beauty.whitening
```

处出现同样的：

```text
ModuleNotFoundError: No module named 'gradio'
```

这说明当前 `hivision` beauty plugin 的 Production import 链仍然依赖 Gradio runtime。为了不继续修改已经验证的上游推理代码，采用：**把 Gradio 作为 Worker runtime dependency 加回 Docker image。**

因此 `requirements-worker.txt` 显式包含：

```text
gradio>=4.43.0
```

这不是为了启动 Gradio Web UI，而是为了满足现有 beauty plugin import 依赖。Docker CMD 仍然只启动 FastAPI/Uvicorn，不会启动 Gradio 服务。

## ONNX Runtime GPU warning

Docker 启动时可能看到：

```text
[W:onnxruntime] GPU device discovery failed ... /sys/class/drm/card0/device/vendor
```

当前项目明确使用 CPU Execution Provider，因此该信息是 ONNX Runtime 在无 GPU 容器环境中的 warning，不是导致 FastAPI 启动失败的原因。

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
    ↓
进程退出
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

Worker Run 结束时再清理 ONNX session，记录 `worker_run_end` RSS，然后退出进程。

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

Worker Run 完成后 Container 退出；下一个用户点击“开始处理”时由 Vercel 再唤醒新的 Lightning Container。

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
 ↓
clear model cache
 ↓
process exit
 ↓
Container stopped / scale-to-zero
```

此前已经实测 3 Job 串行 Worker Run 成功，并确认同一 Worker Run 内模型只实际加载一次。

## Docker 化本阶段修改

已完成：

1. 新增 `.dockerignore`。
2. 保留所有 `.onnx` 模型文件进入 image。
3. Dockerfile 去除重复 FastAPI / multipart / pillow 安装。
4. 新增 `requirements-worker.txt`，将 Production Worker Web/API 依赖与模型依赖分开。
5. Dockerfile 不再安装 `requirements-app.txt`，避免重复安装 Worker Web 依赖。
6. 首次 Docker 启动发现 `hivision` beauty plugin import 链仍然要求 Gradio。
7. 已将 `gradio>=4.43.0` 加回 `requirements-worker.txt`，满足 `grind_skin.py` / `whitening.py` 的现有 import 依赖。
8. 不再修改 beauty plugin 的已验证代码路径。
9. 保持单容器 `uvicorn api_server:app --host 0.0.0.0 --port 8000`。
10. 不修改 `docker-compose.yml`，因为 Production 不使用它。
11. 不修改 Worker Bridge 和推理业务逻辑。
12. 修复 Worker Run 完成后 Uvicorn 主进程继续存活的问题。
13. Worker Run 结束后清理模型并显式退出进程，使 Docker Container 停止并允许 Lightning scale-to-zero。

## 当前待验证

1. 重新执行 Docker build（建议 `--no-cache`）。
2. 确认 `.onnx` 文件全部进入 image。
3. 确认容器可以启动 FastAPI / Uvicorn，且不再出现 `ModuleNotFoundError: gradio`。
4. 确认 `/process-queue` 可以被 Lightning Platform 正常调用。
5. 重新执行完整 3 Job Worker Run。
6. 确认日志顺序为：`finish` → `worker_run_end` → `model cache cleared` → `exiting process for scale-to-zero`。
7. 确认容器进程退出，Lightning 实例最终 scale-to-zero，而不是继续保持运行。
8. 验证下一次开始处理能够重新创建新的 Worker Run / Container 并重新加载模型一次。
9. 验证 heartbeat、lease recovery、fail/retry、duplicate complete、credential expiration。

## 后端当前提交基线

进入 Docker packaging 前的业务基线：`534b25f302ad48a5ece6966aded9a32296bdc337`。

Docker packaging commits：

```text
44135e2e828be6b01be495d1a08971ea57c0352b  # .dockerignore
cdd75d15ddff6caddea687a836f4bfc932c7b0aa  # requirements-worker.txt
df5818a81fcb590c2acc156c180c985e48bca7f5  # Dockerfile
d26367661aac9783bcda51f88b7a9bedcd21be27  # previous attempted beauty-plugin cleanup; superseded by restoring Gradio runtime dependency
b5df019ce4b4b11cc3d6f4c89c3d3ea7bc07d467  # restore Gradio runtime dependency
565b6fe40ddf401fe580f07a83630bf82b03caab  # exit process after Worker Run for Lightning scale-to-zero
```
