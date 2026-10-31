# AI Warning

视频智能报警监测系统 MVP，首期聚焦安全帽检测。

## 当前能力

- 通道和视频源配置，支持 `rtsp`、`gb28181`、`hikvision_nvr`、`onvif` 类型
- 通道级安全帽报警规则
- 一键接入通道（自动创建默认“未戴安全帽”规则）
- 通道 CSV 批量预览与批量导入
- 通道预览接口（支持浏览器可播放地址）
- ONVIF 一键探测并生成流地址
- 规则支持多类型与 ROI 区域（归一化坐标）
- 报警位置提取（bbox/点位）与报警图片留证落盘
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

检测结果上报支持直接附带 `image_base64`，系统会自动落盘图片并记录到事件 `snapshot_path`，同时提取命中目标位置到 `alarm_position`。

## 傻瓜化配置（推荐）

MVP 默认使用项目根目录下的 `aiwarning.db`，无需额外数据库配置。

通道接入建议优先使用一键接口：

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/v1/channels/quick-create" `
  -H "Content-Type: application/json" `
  -d "{\"name\":\"北门摄像头\",\"source_type\":\"rtsp\",\"stream_url\":\"rtsp://example/cam\"}"
```

该接口会同时完成：

1. 创建通道
2. 创建默认未戴安全帽报警规则（`confidence=0.6`、`duration_seconds=1.0`、`cooldown_seconds=60`）

## 通道批量添加（可预览）

先准备 UTF-8 编码 CSV（表头固定）：

```csv
name,source_type,stream_url,preview_url
北门摄像头,rtsp,rtsp://example/cam1,https://example.com/cam1.m3u8
东门摄像头,gb28181,device://34020000001320000001,
```

预览校验：

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/v1/channels/batch-preview" `
  -F "file=@D:\channels.csv"
```

确认导入（可选传 `model_id`，把该模型绑定到批量创建的默认规则）：

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/v1/channels/batch-create" `
  -H "Content-Type: application/json" `
  -d "{\"rows\":[{\"name\":\"北门摄像头\",\"source_type\":\"rtsp\",\"stream_url\":\"rtsp://example/cam1\"}],\"model_id\":1}"
```

## 通道预览

- 添加通道时可选传 `preview_url`（必须是 `http://` 或 `https://`）。
- `GET /api/v1/channels/{id}/preview` 返回该通道是否可浏览器直接预览。
- `rtsp/gb28181` 原始地址通常不能直接在浏览器播放，建议接入转码服务输出 HLS/MP4 地址并填入 `preview_url`。

## ONVIF 一键探测接入

探测并生成可用流地址：

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/v1/onvif/probe-stream" `
  -H "Content-Type: application/json" `
  -d "{\"host\":\"192.168.1.64\",\"port\":80,\"username\":\"admin\",\"password\":\"your-password\",\"transport\":\"RTSP\"}"
```

然后把返回的 `stream_url` 和 `profile_token` 搭配 `source_type=onvif` 调用 `/api/v1/channels/quick-create`。  
管理页面已内置 ONVIF 表单，可直接“探测并一键接入”。

## 多规则与画面范围（ROI）

- 同一通道可多次调用 `POST /api/v1/rules` 添加多条规则（如 `no_helmet`、`intrusion`、`crowding`）。
- 规则可选 `roi_polygon`，格式为归一化坐标点数组（范围 `[0,1]`）：

```json
{
  "channel_id": 1,
  "alarm_type": "intrusion",
  "confidence": 0.6,
  "duration_seconds": 1.0,
  "cooldown_seconds": 60,
  "roi_polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]
}
```

## 报警图片与位置留证

- `POST /api/v1/detections` 额外支持 `image_base64` 字段（可为纯 base64 或 data URL）。
- 创建事件时会自动记录：
  - `snapshot_path`：本地留证图片路径（默认 `event-images` 目录）
  - `alarm_position`：命中目标位置（优先 bbox，其次点位）
- 可通过 `GET /api/v1/events/{id}/image` 查看单条事件的留证图片。

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
