# HivisionIDPhotos Queue Worker

本仓库当前仅运行 FastAPI 队列 Worker，不使用 Docker、Gradio 或旧版表单 API。

## 环境

- Python 3.10 或更高版本
- Linux（内存回收逻辑会在 glibc 环境下尝试调用 `malloc_trim`）
- BiRefNet 与 RetinaFace ONNX 模型

创建并启用项目虚拟环境，然后安装依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

后续测试和启动前都需要先执行 `source .venv/bin/activate`。

下载模型：

```bash
python3 scripts/download_model.py --models birefnet-v1-lite retinaface-resnet50
```

模型应分别位于：

- `hivision/creator/weights/birefnet-v1-lite.onnx`
- `hivision/creator/retinaface/weights/retinaface-resnet50.onnx`

## 启动

```bash
uvicorn api_server:app --host 127.0.0.1 --port 8000
```

如需由反向代理或远程服务访问，请根据网络边界自行调整 `--host`。

## 接口

- `GET /`：服务状态
- `GET /health`：健康检查
- `POST /process-queue`：同步执行一次队列 Worker Run

`/process-queue` 接受 `bridge_url`/`bridgeUrl`、`vercel_origin`/`vercelOrigin`、`worker_run_id`/`workerRunId`、`worker_credential`/`workerCredential`，以及可选的 `max_jobs`/`maxJobs`。同一进程一次只运行一个 Worker Run。

## 检查

```bash
python3 -m unittest discover -s tests
```

项目基于 HivisionIDPhotos，许可证见 [LICENSE](LICENSE)。
