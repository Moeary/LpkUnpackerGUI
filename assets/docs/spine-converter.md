# Spine skeleton converter

应用内置的是上游 [wang606/SpineSkeletonDataConverter](https://github.com/wang606/SpineSkeletonDataConverter) 的 C++ 读取器、写入器和跨版本转换逻辑，并在固定提交上增加了一个很薄的 C ABI。GUI 通过 `ctypes` 直接调用 DLL，不需要 `SpineSkeletonDataConverter.exe`、`SpineAtlasDowngrade.exe` 或 Spine 编辑器安装。

当前固定上游提交为 `5ecb2139b0a1af266974f95abeec6bb8562d1249`（Release `v3.8` 对应源码）。源码和许可证位于仓库的 `third_party/wang606_spine_converter/`；本机编译出的 DLL 位于 `runtime/tools/SpineSkeletonDataConverter/lpk_spine_converter.dll`。`runtime/` 被 Git 忽略，DLL 是当前机器的本地构建产物，不会随源码自动分发。

## 构建内置 DLL

要求 CMake 3.15 或更高版本、支持 C++20 的编译器和 PowerShell。使用项目附带的可复现脚本：

```powershell
pixi run native-converter
```

或直接调用脚本并指定生成器：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File third_party/wang606_spine_converter/build_native.ps1 `
  -Generator "MinGW Makefiles"
```

脚本按生成器把中间文件放在 `runtime/build/spine-converter-mingw/` 或
`runtime/build/spine-converter-msvc/`，并把结果安装到：

```text
runtime/tools/SpineSkeletonDataConverter/lpk_spine_converter.dll
```

也可以通过 `-BuildDirectory` 和 `-OutputPath` 指定临时构建目录与 DLL 目标路径。构建目标名为 `lpk_spine_converter`，ABI 版本为 `1`。导出的函数为：

```text
spine_converter_convert(input_file, output_file, target_version,
                        output_format, remove_curve,
                        error_buffer, error_buffer_size)
```

输入路径、输出路径和目标版本使用 UTF-8；返回 `0` 表示成功，非零值及错误缓冲区内容表示失败。`target_version` 必须是完整的 `x.y.z`，`output_format` 只能是 `json` 或 `skel`。

## GUI 使用说明

选择一个 `.json`/`.skel` 文件，或选择一个只包含唯一目标骨骼的目录。目录中没有骨骼、存在多个无法消歧的骨骼，或模型配置指向多个骨骼时，GUI 会拒绝继续；目录选择不是批量递归转换。手动转换始终可用，GUI 不显示外部 EXE 路径，也不会启动外部转换进程。

目标版本默认是完整版本 `3.8.75`，可在设置中配置。输出格式只能选择 `json` 或 `skel`，没有 `same` 或其他格式选项。默认保留并转换动画曲线；勾选“移除曲线”时，跨 3.x/4.x 转换会舍弃曲线数据。源文件始终保留，转换结果写入独立副本，GUI 会把关联的 atlas 和贴图一并复制到该副本。

版本降级可能丢失目标版本不存在的数据，不能保证无损，也不能保证结果能够还原为 `.spine` 工程。转换器处理的是 skeleton data（JSON/SKEL），不是 Spine 编辑器工程文件或完整项目资产。

目标为 `3.8.75` 时，内置转换器会写出完整的 `3.8.75` 版本字段。能否预览由所选 runtime 是否接受该精确版本决定；编辑器导入和无损往返均不保证。应用不会通过改写版本字符串来伪装兼容性。

## 支持范围与限制

上游转换逻辑支持 `3.5.x`、`3.6.x`、`3.7.x`、`3.8.x`、`4.0.x`、`4.1.x` 和 `4.2.x`。输入版本由骨骼内容自动检测，目标版本必须写完整的三段式版本号。跨版本转换遵循固定提交中的字段转换规则，曲线、旋转、路径约束、约束顺序和版本特有字段可能发生变化；请在目标 runtime 中检查动画和约束效果。

Atlas 由应用后端处理。目标为 3.x 时，正的有限 `scale` 会按 `1/scale` 同步缩放贴图与 atlas 度量，`pma:true` 会反预乘贴图并移除 PMA 标记。无效或非正 `scale`、缩放后为零尺寸或越界的区域、冲突的 scale/PMA 引用，以及除 0/90 度外的 region rotation 会明确拒绝；未知的 3.x atlas 字段也会拒绝，以免静默丢失数据。目标为 4.x 时 atlas 与贴图按原文件复制。缺少关联 atlas 时仍可输出骨骼，但报告会警告运行时需另行提供图集与贴图。

每次转换都会生成 `spine_conversion_report.json`，记录输入/输出版本、输出格式、DLL ABI、固定上游提交、警告和 atlas 处理结果。报告中的 `converter_type` 为 `in-process native DLL`；不存在可用 DLL 时会给出构建脚本路径，不会回退到 EXE。

## 跨机器使用

目标机器可以直接复制已经构建好的 `runtime/tools/SpineSkeletonDataConverter/lpk_spine_converter.dll` 到相同相对路径；也可以复制 `third_party/wang606_spine_converter/`，按上面的命令在目标机器重新构建。重新构建时应保留 `third_party/wang606_spine_converter/LICENSE`、`SOURCE_METADATA.json` 和 `THIRD_PARTY_NOTICES.md`，并核对固定提交。

PowerShell 校验本机 DLL：

```powershell
Get-FileHash .\runtime\tools\SpineSkeletonDataConverter\lpk_spine_converter.dll -Algorithm SHA256
```

当前本机构建 DLL 的 SHA-256 为 `510f49d8e20aad9f4a0d81e5028be34c688962b9aa1d3061dbd4e6bb84c093bb`。不同编译器或编译参数可能产生不同二进制哈希；来源应以固定提交和许可证哈希为准。

## 许可证与来源

上游项目声明使用 **PolyForm Noncommercial License 1.0.0**。完整许可证保存在 `third_party/wang606_spine_converter/LICENSE`，其 SHA-256 为 `75f5f2ae732cdc31adf8ad90a42ed4ed47476b3f27fcdf90de13e79d6788309d`。来源、固定提交、构建目标和 C ABI 记录在 `third_party/wang606_spine_converter/SOURCE_METADATA.json`。再分发或修改时必须保留许可证条款及其要求的通知；该许可证只授予非商业用途范围内的权利。

上游版本差异记录见[版本差异记录](https://github.com/wang606/SpineSkeletonDataConverter/blob/main/%E7%89%88%E6%9C%AC%E5%B7%AE%E5%BC%82%E8%AE%B0%E5%BD%95.md)。
