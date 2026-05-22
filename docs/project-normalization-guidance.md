# LpkUnpackerGUI 项目继续规范化建议

本文基于当前仓库结构、主窗口导航、GUI 页面和 core/tooling 代码职责梳理而成，目标是给后续迭代提供边界和分层建议。本文只描述推荐方向，不要求一次性重构。

## 现状概览

当前项目是一个以 Live2DViewerEX LPK/WPK 解包为起点的 Windows 桌面工具，使用 PySide6/qfluentwidgets 构建 GUI，通过 pixi 管理运行环境，通过 Nuitka 打包。

主要入口和职责如下：

- `app/main.py`：创建 QApplication，初始化 i18n、图标、主窗口。
- `app/gui/MainWindow.py`：集中注册导航页面，目前包括 LPK 解包、Unity 导出、Steam 工坊、原生预览、网页预览、魔改、PSD、加密包占位、设置。
- `app/gui/ExtractorPage.py`：LPK/WPK 批量解包入口，支持文件/文件夹拖拽，调用 `ExtractorThread`。
- `app/gui/UnityExtractorPage.py`：Unity 资源导出入口，调用 `AssetStudioCLI` 导出贴图或 Live2D 结构。
- `app/gui/SteamWorkshopPage.py`：扫描 Steam/Live2DViewerEX 工坊目录，批量提取 LPK。
- `app/gui/PreviewPage.py`、`Live2DPreviewWindow.py`、`Live2DCanvas.py`：原生 Live2D 预览链路，基于 `live2d-py`/OpenGL，输入当前主要是 `*model*.json`。
- `app/gui/WebPreviewPage.py`、`web_server.py`、`assets/live2d/*`：网页 Live2D 预览链路，基于 FastAPI 静态文件挂载、WebSocket 消息和浏览器 PIXI/Live2D runtime。
- `app/gui/PsdReconstructionPage.py`：Live2D PSD 导出/回写入口，调用 `psd_reconstructor`。
- `app/gui/Live2DModPage.py`：Live2D 魔改入口，读取模型 JSON，生成多皮肤切换模型配置。
- `app/gui/EncryptionPage.py`：目前是未实现的占位页面。
- `app/core/*`：解包、WPK、Unity CLI 封装、Cubism Core 访问、PSD 重建、Steam 扫描、配置等领域逻辑混合在一起。
- `app/tools/*`：随包外部工具，目前包含 AssetStudioModCLI 和 CubismCore 开发占位。
- `assets/live2d/*`：网页预览器静态资源与 vendor JS。
- `scripts/*`：构建、发布、源码检查、Cubism drawable sidecar 导出等开发/维护脚本。
- `docs/*`：已有构建、Cubism Core、PSD 重建管线等说明。

## 目标边界

建议把产品边界明确为：面向 Live2D 运行时资源的导入、解包、预览、PSD 图层导出和轻量编辑工具。

应保留并强化的核心能力：

- Live2D 解包：从 `.lpk`、`.wpk`、Unity 加密/未加密资源文件、Unity 资源文件夹中提取 Live2D 模型、纹理、动作、物理、配置等。
- 统一预览：一个预览页面同时承载程序内预览和 web 预览，支持拖入 `.lpk`、`.wpk`、Unity 文件、Unity 文件夹、已解包模型目录、`model3.json`/`model.json`。
- PSD 图层导出：一个 Live2D PSD 图层导出页面，聚焦运行时资源到可检查/可编辑 PSD 的近似重建和图集拆层。
- Live2D 魔改/编辑：一个编辑页面，聚焦模型 JSON、贴图替换、多皮肤切换、动作/HitArea 配置等 Live2D 运行时层面的改动。
- 设置和诊断：保留语言、主题、外部工具路径、临时目录策略、诊断日志入口。

应收束或移除的边界：

- 不做通用 Unity 资源管理器。Unity 入口只服务于 Live2D 资源提取和预览。
- 不做泛用加密包提取器。`EncryptionPage` 的目标应并入“导入/解包”管线，专门处理 Live2D 相关 Unity 加密/未加密来源。
- Steam 工坊不建议长期作为独立一级产品页面；它更适合作为解包页中的“来源扫描器”或高级入口。
- 不承诺恢复原始作者 PSD。PSD 页面应明确输出是运行时资源的近似图层化结果。

## 推荐页面信息架构

建议把一级导航压缩为 5 个用户心智稳定的页面：

1. **解包**
   - 输入：`.lpk`、`.wpk`、Unity 文件、Unity 文件夹、Steam/Live2DViewerEX 工坊目录。
   - 模式：完整解包、仅导出 Live2D、仅导出贴图、批量扫描。
   - 输出：标准化 Live2D 模型目录，包含模型 JSON、moc/moc3、纹理、动作、物理、导出报告。
   - 当前页面归并：`ExtractorPage`、`UnityExtractorPage`、`SteamWorkshopPage`、`EncryptionPage`。

2. **预览**
   - 输入：可直接拖入解包页支持的所有来源，也可选择已解包模型目录或模型 JSON。
   - 渲染器：原生渲染、Web 渲染，作为页面内切换项，不再是两个导航页面。
   - 行为：自动解包到临时模型包，加载模型，释放旧模型，清理临时目录和 web 挂载。
   - 当前页面归并：`PreviewPage`、`WebPreviewPage`、`Live2DPreviewWindow`、`Live2DCanvas`、`web_server.py`、`assets/live2d`。

3. **PSD 图层导出**
   - 输入：模型目录、`model3.json`、`.moc3`、已导出的 PSD。
   - 模式：完整人物/场景 PSD、图集拆层 PSD、PSD 回写图集 PNG。
   - 输出：PSD、`.lpkpsd.json` sidecar、回写贴图。
   - 当前页面保留：`PsdReconstructionPage`，名称建议从“PSD 还原实验”调整为“PSD 图层导出”。

4. **魔改/编辑**
   - 输入：模型目录或模型 JSON。
   - 能力：贴图替换、多皮肤配置、HitArea/动作配置、保存前预览、备份与回滚到页面级备份。
   - 当前页面保留并增强：`Live2DModPage`。

5. **设置**
   - 能力：语言、主题、临时文件策略、外部工具路径、Cubism Core DLL 路径、AssetStudio CLI 路径、日志/诊断。
   - 当前页面保留：`SettingsPage`。

## 推荐分层

### `core`

`core` 应只承载可复用、无 GUI 依赖的领域能力。建议进一步拆成以下子域：

- `core/sources`：统一输入识别，判断 LPK/WPK/Unity/文件夹/模型 JSON/PSD。
- `core/extract`：LPK/WPK/Unity 解包和标准化输出，封装 `LpkLoader`、`WPKHandler`、`AssetStudioCLI`。
- `core/model`：Live2D 模型发现、模型包描述、路径重写、临时模型包生命周期。
- `core/preview`：预览会话抽象，定义加载、卸载、渲染器状态，不直接依赖具体 GUI 控件。
- `core/psd`：PSD 导出、sidecar、回写。
- `core/edit`：模型 JSON 修改、贴图替换、多皮肤配置、动作/HitArea 修改。
- `core/tools`：外部工具发现、版本探测、能力检测。

建议形成一个统一的数据对象，例如：

```text
SourceInput -> ImportPlan -> Live2DPackage -> PreviewSession/PsdExport/EditSession
```

这样 GUI 页面只处理用户交互，core 负责判断“这个输入是什么、需要怎样解包、产物在哪里、何时清理”。

当前第一阶段已经按这个方向落地：

- `app/core/extract/models.py`：定义 `ExtractSourceType`、`ExtractMode`、单项结果和批处理结果，统一记录成功数、失败数、导出数和跳过数。
- `app/core/extract/detector.py`：集中识别 `.lpk`、`.wpk`、Unity 文件和文件夹来源，并提供包目录扫描。
- `app/core/extract/lpk.py`、`wpk.py`、`unity.py`：分别封装 LPK、WPK、AssetStudio/Unity 来源处理。
- `app/core/extract/folder.py`：封装文件夹来源扫描和包目录批处理入口。
- `app/core/extract/batch.py`：作为调度层，按来源类型和模式派发到具体处理器，并输出统一批处理统计。
- `app/core/extractor_thread.py`：保留 Qt 线程壳，但不再直接承载解包业务逻辑。
- `app/gui/ExtractorPage.py`、`UnityExtractorPage.py`：开始读取统一批处理结果，显示成功/失败/导出/跳过统计，并把失败项写入日志。
- `app/gui/MainWindow.py`：一级导航先收敛到解包、预览、PSD 图层、魔改、设置；Unity、Steam、Web 预览、加密页保留代码但不再创建为主界面实例，避免隐藏页面启动无关服务。
- `app/gui/PreviewPage.py`：原生预览页开始承担轻量图片检查能力；普通图片/文件夹直接缩略图预览，Unity 来源只导出到临时目录用于查看，不写入解包输出目录。
- `app/core/model/resolver.py`：新增 `Live2DPackageResolver` 雏形，集中发现 `model3.json`/`model.json`、校验 moc 引用、收集贴图路径，并生成预览用 pretty JSON。
- `app/core/preview/session.py`：新增预览导入会话，优先直接加载模型目录或模型 JSON；对 `.lpk`、`.wpk`、Unity 文件/文件夹会先解包到 `runtime/temp` 再解析模型包。
- `app/gui/PreviewPage.py`：预览页已接入 `core/model` 和 `core/preview`，可直接拖入 LPK/WPK、Unity 来源、模型目录、模型 JSON 或普通图片；非 Live2D 的 Unity 来源会退回到程序内临时图片预览。
- `app/paths.py`、`app/core/settings_manager.py`：新增 `runtime/` 作为运行期数据根目录，默认使用 `runtime/temp`、`runtime/output/<type>` 和 `runtime/setting.json`；设置页可修改输出根目录。

下一步建议把 Steam 工坊扫描、加密/未加密 Unity 来源探测继续接入同一个 `core/extract` 或后续 `core/sources -> core/model` 流程，而不是继续在页面类里新增分支。

### `gui`

`gui` 应按页面和共享控件拆分，避免页面直接承担大量业务逻辑：

- `gui/pages/unpack_page.py`
- `gui/pages/preview_page.py`
- `gui/pages/psd_export_page.py`
- `gui/pages/live2d_edit_page.py`
- `gui/pages/settings_page.py`
- `gui/widgets/*`：拖拽区、日志面板、输出路径选择、模型信息面板、进度任务面板。
- `gui/renderers/native/*`：原生预览窗口、OpenGL Canvas。
- `gui/renderers/web/*`：本地 web 服务、web 预览桥接。

短期不必立即移动文件，但新增代码应朝这个边界靠拢。

### `tools`

建议把外部二进制工具视为可替换 runtime，而不是业务代码的一部分：

- AssetStudioModCLI：用于 Unity 资源导出，保留随包策略，但所有调用必须经过 `core/tools` 或 `core/extract` 封装。
- Cubism Core：因授权原因建议默认不随 release 打包，允许用户配置 DLL 路径，开发树可保留占位说明。
- 后续如引入 Unity 解密 helper，应统一放在 `app/tools/<ToolName>` 或打包根目录 `tools/<ToolName>`，并提供 license/notice。

### `assets`

`assets` 应只放运行时静态资源：

- `assets/app`：图标等桌面资源。
- `assets/live2d`：web 预览器页面和应用逻辑。
- `assets/vendor`：网页预览依赖的前端 runtime。
- `assets/readme`：README 演示图。

网页预览逻辑可以继续放在 `assets/live2d`，但 Python 侧的 web 服务、模型挂载、消息协议不应继续混在页面类里。

### `scripts`

`scripts` 应只放开发、构建、发布、诊断脚本：

- 构建：`build_nuitka.py`
- 发布：`prepare_release.py`
- 检查：`check_sources.py`
- 诊断/导出辅助：`export_cubism_drawables.py`

不建议把用户主流程依赖的功能只放在 `scripts` 中；如果 GUI 和 CLI 都要用，应下沉到 `core`。

### `docs`

`docs` 应承载产品边界、构建、第三方工具、技术限制、用户工作流说明：

- 产品方向：本文。
- 构建发布：`build-actions-guide.md`、`RELEASES.md`。
- PSD/Cubism 限制：现有 PSD 和 Cubism Core 文档。
- 第三方工具政策：建议新增或扩展一个集中说明，列出随包/不随包/用户自带工具。

## 推荐数据流

### 解包数据流

```text
用户拖入/选择输入
  -> SourceDetector 识别类型
  -> ImportPlan 决定处理器
  -> LPK/WPK/Unity extractor
  -> Live2DPackage 标准化输出
  -> 输出目录 + 导出报告
```

关键建议：

- WPK 不应长期只作为“先解压成临时 LPK 再处理”的页面细节，而应是 ImportPlan 的一种来源。
- Unity 文件和 Unity 文件夹应统一由 AssetStudio 导出 Live2D 资源，再进入 Live2DPackage 发现流程。
- 对加密 Unity 文件，先做能力探测：是否 AssetStudio 可直接处理，是否需要外部解密 helper，是否只能提示用户补充工具。

### 统一预览数据流

```text
用户拖入 LPK/WPK/Unity/文件夹/model JSON
  -> SourceDetector
  -> 如有需要，导入到临时工作区
  -> Live2DPackageResolver 找到首选模型 JSON
  -> PreviewSession 创建
  -> NativeRenderer 或 WebRenderer 加载
  -> 切换模型/关闭页面时卸载模型、解除 web 挂载、清理临时工作区
```

内存/临时文件策略：

- 优先内存读写：LPK/WPK 解包阶段尽量使用 bytes/stream 传递中间数据。
- 必须落盘时使用单次预览会话临时目录，因为 `live2d-py`、PIXI runtime、AssetStudio CLI 通常需要真实路径或 HTTP URL。
- 临时目录应由 `PreviewSession` 持有，关闭模型、切换模型、关闭应用时清理。
- Web 预览应支持 unmount 或过期挂载表，避免 `web_server.py` 的模型目录挂载无限增长。
- 原生预览应在模型切换和窗口关闭时显式释放 OpenGL/live2d 对象，避免旧模型纹理和文件句柄残留。

### PSD 导出数据流

```text
模型目录/model3.json/.moc3
  -> Live2DPackageResolver
  -> Cubism Core sidecar 生成或读取
  -> PSD exporter
  -> PSD + .lpkpsd.json
  -> 可选：PSD 回写 atlas PNG
```

关键建议：

- “完整人物/场景 PSD”和“图集拆层 PSD”继续明确区分。
- 回写只承诺 atlas-components 模式，mesh pose PSD 不应默认声明可逆。
- sidecar 是后续编辑和回写的稳定契约，应版本化。

### 魔改/编辑数据流

```text
模型目录/model JSON
  -> EditSession
  -> 读取纹理、动作、HitArea、参数
  -> 用户编辑
  -> 写入到新输出目录或带备份的原目录
  -> 发送到统一预览页验证
```

关键建议：

- 默认不要直接覆盖原模型目录；提供“另存为魔改目录”和“原地修改前备份”两种明确模式。
- 编辑页面应复用统一预览页的 PreviewSession，避免再造预览逻辑。

## 短期路线

建议 1-3 个迭代内完成：

- 合并预览导航：保留一个 `Live2D 预览` 页面，页面内提供原生/Web 渲染器切换。
- 建立 `Live2DPackageResolver`：统一查找 `model3.json`、`model.json`、纹理、动作、物理文件。基础 resolver 已落地，后续应补充物理、动作、HitArea、模型版本和依赖缺失诊断。
- 建立 `SourceDetector`：统一识别 `.lpk`、`.wpk`、Unity 文件、目录、模型 JSON、PSD。
- 让预览页支持直接拖入 LPK/WPK/Unity 文件：先落到临时工作区，完成后加载模型。基础流程已落地，后续需要补进取消、进度和临时目录异常清理。
- 删除或隐藏 `EncryptionPage` 一级导航，把目标能力并入解包页。
- 把 Steam 工坊扫描降级为解包页中的“扫描来源”入口。
- 为 Web 预览增加模型挂载释放/过期机制。
- 给解包、预览、PSD、魔改四个核心流程统一日志格式和错误提示。
- 在 docs 中补充第三方工具和授权矩阵。

## 中期路线

建议 1-3 个月内逐步完成：

- core 层去 GUI 依赖：让解包、模型发现、PSD、编辑逻辑可由 GUI、CLI、测试共同调用。
- 统一任务系统：所有耗时任务使用同一种 worker/job 抽象，统一取消、进度、日志、错误。
- 标准化导出报告：每次解包生成 JSON 报告，记录输入、工具版本、输出模型、失败项、临时目录策略。
- 统一预览会话生命周期：模型加载、旧模型卸载、临时文件清理、web 挂载清理、OpenGL 释放都在同一个 session 中完成。
- 完善 Unity 加密来源策略：明确支持清单、失败原因、用户需要提供的外部工具。
- 强化 PSD sidecar 契约：版本号、schema、兼容策略、回写验证。
- 给魔改/编辑页增加 dry-run 校验、预览确认、输出目录模式。
- 引入测试样本目录或小型 fixture，覆盖 LPK/WPK/Unity/PSD sidecar 关键路径。

## 技术债清单

当前较明显的技术债：

- 页面过多且心智重叠：原生预览和网页预览分裂，Unity 导出和 LPK 解包分裂，Steam 工坊作为一级页面过重。
- GUI 页面承载业务逻辑过多：文件识别、模型 JSON 校验、临时文件、web 广播、PSD 任务等散落在页面类中。
- 输入识别重复：`ExtractorPage`、`UnityExtractorPage`、`PreviewPage`、`WebPreviewPage`、`PsdReconstructionPage` 都有各自的输入判断逻辑。
- 临时文件生命周期不统一：LPK/WPK 解包、预览美化 JSON、web 模型挂载、PSD sidecar 生成各自管理。
- Web 预览挂载表只增不减，长期使用可能积累无效目录引用。
- `LpkLoader.check_decrypt` 仍存在 stdin 交互路径，不适合 GUI 批处理和自动化。
- `ImageExtractor` 里存在为跳过交互而 monkey patch loader 的逻辑，应下沉为正式的非交互解密策略。
- `Live2DModPage` 目前硬编码英文/中文文案和直接写原目录，i18n、备份、输出策略不足。
- `EncryptionPage` 是占位功能，容易误导用户。
- `core` 里同时存在业务、第三方工具封装、配置、Steam、PSD、Cubism 逻辑，缺少子域边界。
- AssetStudio/Cubism Core 工具发现策略分散，授权和打包差异需要集中声明。
- 构建脚本目前只明确包含 AssetStudioCLI，不包含 CubismCore；这是合理策略，但需要在 UI 设置和 docs 中保持一致。

## 打包和第三方工具策略

建议采用“核心应用 + 可替换外部工具”的策略。

### 随包工具

AssetStudioModCLI 可以继续随包：

- 用途：Unity 资源解析、贴图导出、Live2D 资源结构导出。
- 位置：开发环境 `app/tools/AssetStudioCLI`，打包后 `tools/AssetStudioCLI`。
- 要求：保留 `LICENSE.txt`、`NOTICE.txt` 和依赖 DLL。
- 调用：只通过 `AssetStudioCLI` 封装层调用，不让 GUI 拼命令行。
- 诊断：设置页应显示是否找到、版本/路径、一次自检结果。

### 用户自带工具

Cubism Core 建议继续作为用户自带或开发本地工具：

- 原因：授权和分发限制不同于 MIT 工具。
- 发现顺序：环境变量、设置页指定路径、开发树、打包根目录。
- UI 表达：PSD 页面可提示“未配置 Cubism Core 时只能使用降级模式或已有 sidecar”。
- 打包：release 默认不静默包含官方 DLL。

### 可选工具

如果后续支持 Unity 加密文件解密，应按以下原则处理：

- 不把来源不明的解密器混入主程序。
- 每个工具都要有名称、版本、来源、license、输入输出能力说明。
- core 只依赖抽象接口，例如 `UnityDecryptor`，具体实现由工具适配器提供。
- 打包时区分“随包可用”“用户需自行配置”“仅开发诊断可用”。

### 打包原则

- Nuitka 打包只包含运行 GUI 必需的 Python 包、assets、locales、明确允许随包的 tools。
- 不在构建过程中自动下载编译器或第三方二进制工具。
- 构建报告中列出实际包含的外部工具和 license 文件。
- 发布包内建议包含 `THIRD_PARTY_NOTICES.txt`，集中列出 AssetStudio、前端 vendor、Python 包、可选 Cubism Core 策略。

## 推荐命名和导航文案

建议用户可见文案收敛为：

- `解包`
- `预览`
- `PSD 图层导出`
- `魔改/编辑`
- `设置`

建议内部概念命名：

- `SourceInput`：用户拖入或选择的原始输入。
- `ImportPlan`：对输入的处理计划。
- `Live2DPackage`：已标准化的可预览/可导出/可编辑模型包。
- `PreviewSession`：一次预览生命周期，持有临时目录、模型路径、渲染器状态。
- `ToolRegistry`：外部工具发现与能力检测。
- `ExportReport`：解包或导出的结构化报告。

## 验收标准

后续重构可以用以下标准判断方向是否正确：

- 用户拖入 `.lpk`、`.wpk`、Unity 文件或模型目录时，解包页和预览页表现一致。
- 预览页不再要求用户先手动解包到输出目录才能看模型，除非外部工具限制不可避免。
- 切换模型后，旧模型的 native 纹理、web 挂载和临时目录都能释放。
- PSD 页面清楚区分“近似导出”和“可逆回写”的能力边界。
- 魔改/编辑不会默认破坏原始模型目录。
- 新增第三方工具前，先补齐工具发现、授权说明、打包策略和错误提示。
