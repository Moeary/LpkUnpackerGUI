# Spine 预览

统一资源预览识别包含 Spine skeleton、atlas 和贴图的文件夹或 LPK/WPK。程序不捆绑 Spine runtime；需要动画预览时，在预览页的 **Spine runtime** 选择框中选择用户已取得授权的官方 `spine-ts` core/webgl 目录。目录可以包含 `spine-core.js` 与 `spine-webgl.js`，也可以由 `spine-webgl.js` 提供合并构建。

## 版本门禁

当前把 Spine 3.8 和 4.0 作为动态 runtime 族，要求 skeleton 的主次版本与 runtime 相同，例如 `skeleton.spine: 3.8.99` 只能配 3.8 runtime，`skeleton.spine: 4.0.37` 只能配 4.0 runtime。未配置 runtime、Spine 2.1，或尚未完成真实验证的 4.2 资源会明确降级为 atlas 页面预览；不会用不匹配的 runtime 强行解析。若用户已经选择 runtime 目录，但目录缺少可验证版本、脚本或匹配的 core/webgl，程序会报告可修正的配置错误，不会静默降级。

设置可以指向包含多个版本子目录的统一根目录（例如 `runtime/tools/spine/`）。预览根据 skeleton 的主次版本选择 `3.8/` 或 `4.0/` 清单；版本目录仍会做显式校验，不会把 3.8 runtime 误配给 4.0 skeleton。

本机验收用的官方 runtime 位于被 `.gitignore` 忽略的 `runtime/tools/spine/3.8/`，不会进入仓库提交。它来自官方 [spine-runtimes 3.8 分支](https://github.com/EsotericSoftware/spine-runtimes/tree/3.8)，来源 commit 为 `8b4844bd4b193ba9e54487ed397a777993cbad56`，下载归档 URL 为 `https://codeload.github.com/EsotericSoftware/spine-runtimes/zip/refs/heads/3.8`，归档 SHA-256 为 `986DAA9AD21E70BA3D85EB0B6CEEE0C032D9792B0DC4B52E543134209C10A866`。官方许可证保存在 `runtime/tools/spine/3.8/LICENSE`（SHA-256 `6142EE6CC2C03D3A918793E4750AE772BD3755C534D4A35E559E301ACF51EC39`），清单 `runtime/tools/spine/3.8/spine_runtime.json` 同时记录来源与构建文件哈希。运行时的授权和再分发条件以官方 [Spine Runtimes License Agreement](https://github.com/EsotericSoftware/spine-runtimes/blob/3.8/LICENSE) 为准；下载到本机用于开发验收不代表应用获得商业再分发授权。

官方 3.8 `spine-ts` README 说明 `build/spine-core.js` 与 `build/spine-webgl.js` 均为可独立使用的构建，WebGL 构建已经包含 core。清单仍记录两份构建，但以 `combined: true` 告知网页只加载 `spine-webgl.js`，避免重复执行 core。实际页面使用官方 `spine.webgl.AssetManager`、`spine.webgl.Shader`、`spine.webgl.PolygonBatcher`、`spine.webgl.SkeletonRenderer`、`spine.TextureAtlas` 和 `spine.SkeletonJson` API；3.8 的 `AssetManager.getErrors()` 返回对象，因此页面通过 `hasErrors()`/对象值读取错误。

本机验收用的官方 4.0 runtime 位于被 `.gitignore` 忽略的 `runtime/tools/spine/4.0/`；当前预构建文件来自官方 npm 包 `@esotericsoftware/spine-webgl@4.0.31`，并非在本机重新构建。npm registry 元数据 URL 为 `https://registry.npmjs.org/@esotericsoftware%2fspine-webgl/4.0.31`，tarball URL 为 `https://registry.npmjs.org/@esotericsoftware/spine-webgl/-/spine-webgl-4.0.31.tgz`，npm integrity 为 `sha512-G6j31+caQJck/4UN8TVaTKnU0RPysI7ECMkCxcXBGsTmv98m0O5Wx18YgeIf//Bg8KAO+mZ/DmwzeScwGG9HPA==`，tarball SHA-256 为 `FDFE7FC72B870A4DA238F349634DD043390B5035DBCE6782E7E4288ADC6648A1`。构建产物 `spine-ts/spine-webgl/dist/iife/spine-webgl.js` 的 SHA-256 为 `D82E2C32D2FBE2D47D461DA5B8DD3B9DAAC1FBFEDA74E6476114D5D70619185A`；该哈希与此前官方 [spine-runtimes 4.0 分支](https://github.com/EsotericSoftware/spine-runtimes/tree/4.0) 构建记录一致，分支参考 commit 为 `425ce416bb218b28caeec47b317aa57cd7140375`，分支归档 SHA-256 `859B6B7FC3574F2D29008E4942CCB54C08086B577D5E898A6F8A37152AEFA6B3` 仅作来源参考。官方许可证保存在 `runtime/tools/spine/4.0/LICENSE`（SHA-256 `6142EE6CC2C03D3A918793E4750AE772BD3755C534D4A35E559E301ACF51EC39`），清单 `runtime/tools/spine/4.0/spine_runtime.json` 记录 npm 来源、构建文件和哈希。4.0 runtime 的授权和再分发条件以官方 [Spine Runtimes License Agreement](https://github.com/EsotericSoftware/spine-runtimes/blob/4.0/LICENSE) 为准。

官方 4.0 `spine-ts` README 说明 `spine-webgl` IIFE 同时包含 core 和 WebGL，并将 `AssetManager`、`Shader`、`PolygonBatcher`、`Matrix4`、`SkeletonRenderer` 等类导出到顶层 `spine`。4.0 的 `TextureAtlas` 先解析文本，再由调用方对每个 page 调用 `setTexture`；网页按清单里的 `runtime.api: "flat"` 选择该路径。3.8 仍使用 `spine.webgl` 命名空间和带纹理回调的 `TextureAtlas`，清单标记为 `runtime.api: "legacy"`。两种构建都通过 `hasErrors()`/`getErrors()` 显式检查资源加载错误。

真实 3.8 样本 `runtime/test_samples/modern_38/.../skeleton_0.json` 的版本为 `3.8.99`；本机实际 app server 与浏览器已加载该样本及官方 3.8 runtime，渲染角色和背景共一张 atlas、一张纹理，识别 7 个动画，且已验收 `idle` 切换、Pause 和时间滑块推进。4.0 binary skeleton 使用官方 `spine.SkeletonBinary` 解析，4.0 JSON 使用 `spine.SkeletonJson` 解析。页面通过 `document.body.dataset.previewState` 暴露 `loading`、`ready`、`error` 三种状态：runtime 模式在首帧绘制后变为 `ready`，atlas 模式在所有页面图片加载完成后变为 `ready`，加载失败时变为 `error`。导出 PSD 的错误只更新按钮区域状态，不会把已有预览改为错误状态。

## 入口与 manifest

PreviewPage 在后台线程提取源文件到 `runtime/temp` 下的一次性目录，然后只读挂载资源并打开本地网页。动态 manifest 由 Python 生成，包含：

- `skeleton`：JSON 或 binary skeleton URL；
- `skeletonFormat`：`json` 或 `binary`；
- `atlases`、`textures`：atlas 文本和页面图片 URL；
- `skins`、`animations`、`attachments`：用于控制栏和附件列表；
- `runtime.core`、`runtime.webgl`、`runtime.family`：用户选择的匹配 runtime URL。

不应手写 manifest 绕过版本门禁。源文件与用户 runtime 都保持只读，过期导入结果和仍在写入的临时目录会在后台线程完成后再清理。

## 动态控制与姿态导出

3.8/4.0 动态页面提供 skin、animation、暂停、时间点和附件列表控制。循环动画的时间滑块显示 `TrackEntry.getAnimationTime()`，不会因累计 `trackTime` 超过动画时长而停在末端。**Export layered PSD** 通过当前 WebGL 画面按可见 slot 逐层栅格化，再调用 Python 的 `export_spine_pose_psd` 创建 PSD；姿态导出不会修改 skeleton、atlas 或 runtime。

该导出是受限的轴对齐栅格投影：mesh、裁剪、加权变换、特殊混合等无法安全投影的特性会在页面和服务端明确阻止当前姿态导出，不会静默改变外观。网页最多发送 64 个当前 draw order 中的可见附件层，服务端限制总 PNG 负载；Python 端会裁切每层透明边界，并限制画布和累计图层像素，避免全画布层造成无界内存占用。

没有匹配 runtime 时，atlas 页面仍可查看多页纹理；这一路径不提供原生动画姿态。
