# Spine skeleton converter

当前开发机手动安装的是上游项目 [wang606/SpineSkeletonDataConverter](https://github.com/wang606/SpineSkeletonDataConverter) 的 Windows Release `v3.8`。这些 EXE 位于本机 `runtime/tools/SpineSkeletonDataConverter/`；`runtime/` 和 `*.exe` 被 Git 忽略，因此不会随源码分发。Release 发布时间为 2026-07-19，源码提交为 `5ecb2139b0a1af266974f95abeec6bb8562d1249`。当前安装目录还保留：

- `runtime/tools/SpineSkeletonDataConverter/SpineSkeletonDataConverter.exe`
- `runtime/tools/SpineSkeletonDataConverter/SpineAtlasDowngrade.exe`（处理 atlas 的配套工具）
- `runtime/tools/SpineSkeletonDataConverter/LICENSE`
- `runtime/tools/SpineSkeletonDataConverter/SOURCE_METADATA.json`（下载地址、Release digest 与本地 SHA-256）

## Skeleton CLI

```text
SpineSkeletonDataConverter.exe <input_file> <output_file> [options]
```

输入与输出格式由扩展名决定：`.json` 是 Spine JSON，`.skel` 是 Spine 二进制格式。输入 Spine 版本由文件内容自动检测；不传 `-v` 时输出版本默认为输入版本。跨版本转换时，目标版本必须写完整的 `x.y.z`，例如 `3.8.99`，不能只写 `3.8`。

```powershell
& runtime/tools/SpineSkeletonDataConverter/SpineSkeletonDataConverter.exe `
  input.json output.skel -v 3.8.99
```

可用选项：

- `-v x.y.z`：指定输出 Spine 版本。
- `--remove-curve`：跨 3.x/4.x 转换时移除动画曲线，而不是尝试转换曲线。
- `--help`：显示用法。

上游当前列出的版本范围为 3.5.x、3.6.x、3.7.x、3.8.x、4.0.x、4.1.x 和 4.2.x。`SpineSkeletonDataConverter.exe --help` 已在本机成功运行并返回 0；未对大批样本执行转换。

## GUI 使用说明

选择一个 `.json`/`.skel` 文件，或选择一个只包含一个目标骨骼的目录。目录选择如果找不到骨骼，或发现多个 `.json`/`.skel` 而无法确定唯一目标，GUI 会拒绝继续并要求用户明确选择；目录模式不是批量递归转换。源文件始终保留，输出写入独立副本。

正常预览的目标版本默认使用完整版本 `3.8.75`。精确的 `3.8.75` 不能使用当前官方 3.8 分支 runtime；应用设置中必须选择专用的历史 `spine-ts` runtime。当前机器的历史 runtime 位于 `runtime/tools/spine/3.8.75/`，来自官方提交 `c0699e23a0c8799710323bdf0e076e18f6ba41a2`，已用 JSON 与 SKEL 样本分别验收加载、动画、暂停和 seek。它不是官方 `3.8.75` tag，跨机器使用前仍应按本文来源与哈希复核。输出格式只能选择 JSON 或 SKEL，没有 `same` 或其他格式选项。默认转换动画曲线；需要舍弃曲线时勾选“移除曲线”，由 GUI 传递 `--remove-curve`。

GUI 需要提示：版本降级可能丢失目标版本不存在的数据，不能保证无损，也不能保证结果能够还原为 `.spine` 工程。转换器处理的是 skeleton data（JSON/SKEL），不是 Spine 编辑器工程文件、图片工程或完整项目资产。

## 版本差异与限制

上游的[版本差异记录](https://github.com/wang606/SpineSkeletonDataConverter/blob/main/%E7%89%88%E6%9C%AC%E5%B7%AE%E5%BC%82%E8%AE%B0%E5%BD%95.md)列出了字段和语义变化。使用时尤其要注意：

- 3.8 与 4.x 的曲线表示不同；4.x 曲线控制点与端点数值相关，3.8 使用固定端点的四个曲线键。跨越 3.x/4.x 时，曲线转换可能改变动画表现；`--remove-curve` 会明确舍弃这些曲线。
- 当前官方 3.8 分支 runtime 在 `runtime/tools/spine/3.8/spine-ts/build/spine-webgl.js` 的 JSON/SKEL 读取路径（约第 5555/4143 行）硬编码拒绝 `3.8.75`。精确 `3.8.75` 预览应选择设置中的专用历史 runtime；该历史构建来自拒绝检查加入前的官方提交，构建文件没有删除检查或篡改版本字符串。不能通过改写 JSON/SKEL 中的版本字符串来伪装转换或绕过兼容性检查。
- 3.x 与 4.x 的旋转语义不同。4.x 可以表达相对 setup pose 的连续大角度旋转，3.x 按相邻关键帧的最短路径解释；降级时可能需要插入辅助 key，无法承诺逐帧等价。
- 3.8、4.0、4.1、4.2 之间有字段重命名、字段合并/删除、默认值变化和约束枚举差异；3.7 及更早版本也存在曲线、默认值和 skin/constraint 字段差异。
- 转换器支持的骨骼文件边界是 `.json` 与 `.skel`。GUI 会把关联 atlas 和贴图复制到独立输出目录；目标为 3.x 时，正的有限 `scale` 会按 `1/scale` 同步缩放贴图与 atlas 度量，`pma:true` 会反预乘贴图并移除 PMA 标记。无效或非正 `scale`、缩放后变为零尺寸或越界的区域、与同一贴图冲突的 scale/PMA 引用，以及除 0/90 度外的 region rotation 会明确拒绝；未知的 3.x atlas 字段也会拒绝，以免静默丢失数据。目标为 4.x 时 atlas 与贴图按原文件复制。缺少关联 atlas 时仍会输出骨骼，但报告会警告运行时需另行提供图集与贴图。

历史 3.8.75 runtime 来源与验证：

- 官方提交：[c0699e23a0c8799710323bdf0e076e18f6ba41a2](https://github.com/EsotericSoftware/spine-runtimes/commit/c0699e23a0c8799710323bdf0e076e18f6ba41a2)，日期为 2019-12-19；对应归档地址为 `https://github.com/EsotericSoftware/spine-runtimes/archive/c0699e23a0c8799710323bdf0e076e18f6ba41a2.zip`。
- 当前官方 3.8 分支提交 `8b4844bd4b193ba9e54487ed397a777993cbad56` 和其构建文件明确拒绝精确 `3.8.75`；`c0699e23...` 位于该拒绝检查加入前。历史构建不含 `3.8.75` 拒绝字符串，且未对文件内容做版本字符串重写。
- 本机历史 runtime 清单 `runtime/tools/spine/3.8.75/spine_runtime.json` 记录了来源、许可证与构建文件 SHA-256：`spine-core.js` 为 `95a70048378f2e2705a385e6f30c46ee49ac6af94a50a736bd0d6bb395c5aeba`，`spine-webgl.js` 为 `1c51e83db80fe146cd270eda3375f179a1b0b0477f6ebe7c242e64cc51dbb232`，许可证为 `6142ee6cc2c03d3a918793e4750ae772bd3755c534d4a35e559e301acf51ec39`。JSON 与 SKEL 各一份样本均已独立验收，结果可加载并操作动画控制。

配套 atlas 用法：

```text
SpineAtlasDowngrade.exe <input_atlas> <output_dir>
```

该程序没有独立的 `--help` 选项；传入 `--help` 或缺少参数时会打印上述用法并返回 1。它可作为单独的命令行工具使用；GUI 的 atlas 流程仍以应用后端的实际实现和限制为准。

## 跨机器安装

1. 打开 [SpineSkeletonDataConverter v3.8 Release](https://github.com/wang606/SpineSkeletonDataConverter/releases/tag/v3.8)，下载 `SpineSkeletonDataConverter.exe`；需要手动处理 atlas 时再下载 `SpineAtlasDowngrade.exe`。
2. 在目标机器创建任意本地工具目录。若应用使用默认路径，可复制当前机的整个 `runtime/tools/SpineSkeletonDataConverter/` 目录；至少应同时保留 `LICENSE` 和 `SOURCE_METADATA.json`。
3. 若应用没有默认工具目录，可在设置或转换操作中手动选择 `SpineSkeletonDataConverter.exe` 的路径，不需要注册表安装。
4. 用 PowerShell 校验下载文件的 SHA-256，并与本文“来源校验”表及 `SOURCE_METADATA.json` 比较：

   ```powershell
   Get-FileHash .\SpineSkeletonDataConverter.exe -Algorithm SHA256
   ```

   Release 资产的下载地址、大小和预期哈希以 `SOURCE_METADATA.json` 为准；不要从非官方镜像取得 EXE。

若目标机器还要预览精确的 `3.8.75`，需另外复制或按本文来源取得 `runtime/tools/spine/3.8.75/`，在设置页把 Spine runtime 指向该目录（或包含它的统一 runtime 根目录）。该目录不是 Spine 编辑器安装目录，也不能用 Java 编辑器 JAR 代替 `spine-ts` 的 `spine-webgl.js`；当前官方 3.8 分支目录 `3.8/` 仍会拒绝精确 `3.8.75`。

## 许可证

上游项目声明使用 **PolyForm Noncommercial License 1.0.0**。完整许可证随可执行文件保存在 `runtime/tools/SpineSkeletonDataConverter/LICENSE`，许可证正文和上游来源见 [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/)。再分发或修改时必须保留许可证条款及其要求的通知；该许可证只授予非商业用途范围内的权利。

## 来源校验

本地文件与 GitHub Release API 提供的 SHA-256 digest 一致：

| 文件 | 大小 | SHA-256 |
| --- | ---: | --- |
| `SpineSkeletonDataConverter.exe` | 1,587,712 bytes | `b2ca82e46f1f4ca463abf0ccfab32e3c01eb0dd89fc7289b6478f728ca8ed68a` |
| `SpineAtlasDowngrade.exe` | 310,272 bytes | `116a2c515650fde2077c8f679ff5680afb584fc0aa47279fe589f181377d72ce` |

下载地址和许可证哈希也记录在 `SOURCE_METADATA.json`，便于后续升级时复核来源。
