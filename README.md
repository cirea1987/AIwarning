# AI Warning

视频智能报警监测系统 MVP，首期聚焦安全帽检测。

## 当前能力

- 通道和视频源配置，支持 `rtsp`、`gb28181`、`hikvision_nvr` 类型
- 通道级安全帽报警规则
- 报警事件创建、查询、确认和关闭
- 模型登记与上传，支持 ModelScope 仓库地址及 ONNX/PyTorch 模型文件
- 统一的检测结果接口，可由真实模型服务或视频采集器调用
- SQLite 开箱即用，后续可切换 PostgreSQL
- 简单管理端页面，便于验证 API 和业务闭环

## 本地运行

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

打开 <http://127.0.0.1:8000/>，API 文档位于 <http://127.0.0.1:8000/docs>。

## 接入说明

当前工程提供统一视频源和检测结果 API。实际部署时：

1. 视频采集器从 RTSP、GB/T 28181 或海康录像机通道取得帧。
2. 采集器调用 `POST /api/v1/detections` 提交检测结果。
3. API 根据通道规则执行持续时间、置信度和冷却判定，必要时创建报警事件。
4. 后续可将采集器替换为 FFmpeg/GStreamer worker，并将 SQLite 替换为 PostgreSQL。

## 上传安全帽模型

先登记 ModelScope 模型来源，再上传已导出的模型文件：

```powershell
$model = Invoke-RestMethod http://127.0.0.1:8000/api/v1/models -Method Post `
  -ContentType 'application/json' `
  -Body '{"name":"damoyolo safety helmet","framework":"modelscope","source":"https://www.modelscope.cn/models/iic/cv_tinynas_object-detection_damoyolo_safety-helmet","labels":["helmet","no_helmet"]}'

curl.exe -X POST "http://127.0.0.1:8000/api/v1/models/$($model.id)/file" `
  -F "file=@D:\models\safety-helmet.onnx"
```

上传接口目前接受 `.onnx`、`.pt`、`.pth`、`.torchscript` 和 `.zip`。登记模型后，在创建报警规则时把 `model_id` 传入即可。模型文件只保存到本地 `models` 目录；生产环境应挂载独立模型卷，并由推理 worker 校验模型格式后加载。

模型推理不在 Web API 进程中执行；这样可以在 CPU、ONNX Runtime 或 NVIDIA TensorRT 部署之间切换，而不改变业务接口。
