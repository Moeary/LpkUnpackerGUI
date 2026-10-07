# 本地 AI 动画编辑 MCP

AI 客户端可通过本地 MCP 工具检查模型、新建或克隆动作、读取与修改关键帧，再保存为独立模型包。原始文件保持不变；生成副本可以放回程序的资源预览页播放。

服务使用官方 Python MCP SDK 的 `FastMCP`，依赖为 `mcp>=1.28,<2`，当前锁文件为 1.30.0，兼容项目的 Python 3.13。SDK v2 已更名为 `MCPServer`，因此这里保留明确的版本上限。[官方 v1 文档](https://py.sdk.modelcontextprotocol.io/v1/)、[官方迁移说明](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/migration.md)。

## 启动和连接

打开“工具设置 → AI / MCP”。此分类按传输方式、连接与服务、AI 指南分为三张卡片。选择与源模型目录分开的“输出工作目录”；输入模型可以在其他目录，生成的新模型包必须保存到输出工作目录内。

默认使用本地 Streamable HTTP：端口默认 `8765`，可改为 `1..65535` 的可用端口。点击“启动服务”，等待“运行中”，再复制地址或生成客户端配置。服务实际仅监听 `127.0.0.1`，路径固定为 `/mcp`。端口占用会显示具体地址和错误，可查看日志；更改端口前先停止服务。停止服务或正常退出程序会关闭子进程；应用意外退出时，父进程管道的 EOF 也会令服务停止。未保存的内存项目会随服务停止而丢失。

HTTP 客户端配置示例（不同客户端的传输名称与格式可能不同）：

```json
{
  "mcpServers": {
    "live2d-spine-animation": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

选择 stdio 时，端口和应用内启动按钮隐藏；AI 客户端负责启动与停止进程。配置中的解释器或可执行文件是当前程序的路径；源码模式附带绝对 `PYTHONPATH`，无需客户端从项目目录启动。生成配置不会自动修改客户端设置。

配置采用常见的 `mcpServers` JSON 格式，各客户端使用自己的配置格式。下面的 JSON 适用于支持此格式的客户端，需要替换为本机路径；它不是 Codex `config.toml` 内容。

```json
{
  "mcpServers": {
    "live2d-spine-animation": {
      "command": "D:\\Code\\Python\\LpkUnpackerGUI\\.pixi\\envs\\default\\python.exe",
      "args": ["-m", "app.mcp_server", "--workspace", "D:\\AnimationEdits"],
      "env": {
        "PYTHONPATH": "D:\\Code\\Python\\LpkUnpackerGUI"
      }
    }
  }
}
```

先在项目目录安装依赖，并创建或选择与源模型目录分开的输出工作目录：

```powershell
pixi install
New-Item -ItemType Directory -Force D:\AnimationEdits
pixi run mcp-animation --help
pixi run mcp-animation --workspace D:\AnimationEdits
# HTTP 手动启动；Ctrl+C 停止
pixi run mcp-animation --workspace D:\AnimationEdits --transport streamable-http --port 8765
```

也可使用统一入口 `pixi run python -m app.main --mcp-animation --workspace D:\AnimationEdits`。打包程序的入口为 `LpkUnpackerGUI.exe --mcp-animation --workspace D:\AnimationEdits`；本次未构建并实测新的打包程序。

CLI 默认仍为 stdio；直接在终端启动后会等待 MCP 请求。`--transport streamable-http --port 8765` 启用本地 HTTP；`--managed` 专用于应用内子进程，收到 stdin 的 `stop` 或 EOF 就退出。诊断写入 stderr；stdio 协议 stdout 具有独立管道，Python 输出、原生 DLL 输出均转入 stderr。`--log-level DEBUG|INFO|WARNING|ERROR` 控制诊断级别，默认为 `WARNING`。

## AI Markdown 指南

“查看指南 / 复制 Markdown / 另存指南”生成可直接交给 AI 的完整 Markdown，其中填入当前传输、实际端口与地址、绝对输出目录、当前解释器或打包程序路径、通用客户端配置，以及从已注册工具生成的完整 JSON schema。复制和另存保留同一份 UTF-8 Markdown 内容；需要换电脑或目录时，应重新生成配置与指南。

连接 MCP 后，客户端也可以通过 `resources/list` 找到 `lpk-animation://guide`，再用 `resources/read` 读取 `text/markdown` 指南。它提供相同的连接信息、文件边界、工作流和运行时限制；`tools/list` 仍是工具 schema 的权威来源。

仓库内 [animation-ai-guide.md](animation-ai-guide.md) 是含占位符的模板，不能直接作为本机配置。设置页和 MCP resource 返回的是已填入实际路径与实时 schema 的版本。输入读取并不局限于 workspace；仅输出路径受该目录约束，指南对此有明确说明。

## 工具与数据

工具参数和返回值均为 JSON。路径可传绝对路径；相对输入与输出路径均以 `--workspace` 为基准。输入可以位于工作目录之外，输出只能是工作目录以内的新目录，包括解析符号链接或 Windows junction 后的路径。

| 工具 | 主要参数 | 返回内容 |
| --- | --- | --- |
| `inspect_model` | `model_path`, `parameter_inventory_path?` | 类型、版本、参数范围、骨骼、约束、现有动作与可编辑通道 |
| `create_project` | `model_path`, `parameter_inventory_path?` | `project_id` 与 `model` 检查结果 |
| `inspect_project` | `project_id` | 当前 `model` 检查结果 |
| `create_animation` | `project_id`, `name`, `duration`, `loop=false`, `fps=30` | 新动作摘要 |
| `clone_animation` | `project_id`, `source_name`, `new_name` | 克隆动作摘要 |
| `get_keyframes` | `project_id`, `animation_name`, `target`, `channel` | 关键帧、`editable`、不可直接合并的原因 |
| `set_keyframes` | 同上，另加 `keyframes`, `mode="replace"` | 改后的通道关键帧 |
| `save_package` | `project_id`, `output_dir` | 包目录、模型/骨架、动作文件、清单及警告的路径 |
| `close_project` | `project_id` | 释放内存项目；尚未保存的修改随之丢弃 |

项目保存在服务内存中，`project_id` 只在本次服务进程中有效，同时最多 32 个项目。已保存的包可作为 `model_path` 重新载入。工具不接受代码、命令或远程执行参数。

时间使用秒，必须在 `[0, duration]` 内严格递增。数值必须有限；布尔值不能用作数值。关键帧插值为 `linear` 或 `stepped`，描述该帧到下一帧的插值。`mode="replace"` 替换所选通道，`mode="merge"` 按时间合并，同一时间的新帧替代旧帧。其他通道保留。

现有 Bezier 或反向阶跃通道会标记为 `editable=false`，拒绝 `merge`；明确传入 `replace` 可将此通道改写为当前支持的插值。新动作名称不得与已有动作或 Live2D 动作组同名。

## Live2D

支持含 `FileReferences` 的 Cubism Version 3 模型 JSON，常见名称为 `.model3.json`，也可以是 `.model0.json`。参数 ID、最小值、最大值与默认值由本机 Cubism Core 从 `.moc3` 读取；使用已有 `.cdi3.json` 补充参数名称。若原生 Core 不可用，需要与 MOC SHA256 匹配的参数清单文件，此时结果会注明 `inventory_source="sidecar"` 和警告。保存的副本自动包含该清单，便于重新打开。

清单格式如下；范围应来自该模型的实际参数检查，哈希仅确认它对应相同的 MOC 文件。

```json
{
  "format": "LpkUnpacker.Live2DParameterInventory",
  "version": 1,
  "moc_sha256": "MOC 文件的完整 SHA256",
  "parameters": [
    {"id": "ParamAngleY", "min": -30, "max": 30, "default": 0, "name": "头部上下"}
  ]
}
```

`target` 使用清单中的参数 ID，`channel` 固定为 `value`，帧为 `{ "time": 0.5, "value": -12 }`，数值是参数的绝对值。已存在的动作以 `Group[index]` 命名，例如 `Idle[0]`；新建 `ai_nod` 会成为 `FileReferences.Motions.ai_nod[0]`。保存时产生合法 `.motion3.json` 并更新副本的引用。

MCP 只能驱动模型已经具备的参数。模型没有腿部或蹲姿参数时，仅改头部/身体角度无法生成真正的下蹲，也不会新增 ArtMesh、绑定或模型参数。

## Spine 和动作示例

支持 Spine `3.8.x`、`4.0.x` 的 skeleton JSON、`.skel`、`.skel.bytes`，以及引用它们的模型包装 JSON。二进制骨架先经项目的原生转换器转为同版本 JSON，保存副本继续使用该版本 JSON；转换器缺失会返回明确错误。

`target` 必须使用检查结果中的骨骼名称。`translate` 帧使用 `time/x/y`，数值是相对于 setup pose 的位移；`rotate` 使用 `time/angle`，单位为度，也是 setup pose 的偏移；`scale` 使用 `time/x/y`，数值是 setup scale 的乘数。服务统一接收 `angle`，内部会按 3.8/4.0 的各自格式编码。

下例演示一个含 `hip` 骨骼的模型如何降低髋部，再恢复。实际下蹲还需根据该模型的大腿、小腿和 IK 约束调整骨骼；先检查 inventory，再选择目标，不应猜测骨骼名称。向 AI 客户端提出的任务可以是：“检查这个模型的骨骼与 IK，创建 2.4 秒的下蹲、停留、恢复动作，保存为工作目录中的新包，并给出预览模型路径。”

以下 JSON 分别作为对应工具的参数，每一步的 `<project_id>` 来自 `create_project`：

```json
{"model_path": "D:\\Models\\spineboy\\spineboy.json"}
```

调用 `create_project`，接着调用 `create_animation`：

```json
{"project_id": "<project_id>", "name": "ai_crouch", "duration": 2.4, "loop": false, "fps": 30}
```

调用 `set_keyframes`：

```json
{
  "project_id": "<project_id>",
  "animation_name": "ai_crouch",
  "target": "hip",
  "channel": "translate",
  "mode": "replace",
  "keyframes": [
    {"time": 0, "x": 0, "y": 0},
    {"time": 0.8, "x": -25, "y": -100},
    {"time": 1.6, "x": -25, "y": -100},
    {"time": 2.4, "x": 0, "y": 0}
  ]
}
```

最后调用 `save_package`：

```json
{"project_id": "<project_id>", "output_dir": "spineboy_ai_crouch"}
```

输出包保留引用的 atlas、图片、音频与已有动画；所选通道会写入副本 skeleton JSON，另附 `lpk_animation_project.json` 清单。已有目标一律拒绝覆盖，包括空目录。Spine 的动作时长由最后一帧定义，必要时补保持帧；`loop` 与 `fps` 保存在项目清单，包装模型的 `wrap_mode` 也会更新。裸 skeleton 的循环播放由使用它的播放器决定。

打开返回的 `model_path`（或 `skeleton_path`）进行预览，选择新动作即可播放。此流程编辑运行时动作，不生成 Cubism `.cmo3` 或 Spine `.spine` 作者工程。

## 验证

```powershell
pixi run python -m unittest tests.test_mcp_launch tests.test_animation_mcp tests.test_animation_mcp_http tests.test_mcp_settings tests.test_settings_page_layout tests.test_editor_navigation tests.test_build_nuitka -v
pixi run check
```

测试使用官方 `stdio_client` / `streamable_http_client` / `ClientSession` 启动实际子进程，执行 initialize、list_tools、resources/list、resources/read、call_tool，检查 Live2D/Spine 导出、克隆、合并、源文件保留、目录越界与 junction 拒绝，以及 Python/原生 stdout 隔离。HTTP 验证实际端口、端口占用错误、父管道 EOF 与停止命令、监听端口释放、非本地 Host/Origin 拒绝；Qt 验证真实子进程关闭回收、端口偏好保存、stdio 隐藏端口，以及指南查看/复制/另存内容一致。

真实模型可再通过 `scripts/verify_animation_mcp.py --workspace <新工作目录> --spine <Spine模型> --reference-spine <参考Spine模型> --live2d <Live2D模型>` 走协议验收，报告保存为 `acceptance.json`。指定的模型须具有脚本使用的骨骼或参数；该脚本针对项目当前 Belfast、Spineboy、Diana 样本。自动测试中的 Live2D 清单 fixture 不调用无效 MOC 的原生 DLL。
