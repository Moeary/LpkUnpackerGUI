# Release Guide

## 自动发布流程

本项目使用 GitHub Actions 自动编译和发布版本。

### 触发发布

有两种方式触发自动编译和发布：

#### 方法 1: 通过 Git Tags（推荐）

```bash
# 更新版本信息
python scripts/prepare_release.py 1.1.0

# 提交更改
git add .
git commit -m "Release v1.1.0"

# 创建和推送标签
git tag v1.1.0
git push origin main
git push origin v1.1.0
```

标签推送后，GitHub Actions 会自动：
1. 检出代码
2. 安装依赖
3. 使用 Nuitka 编译
4. 创建 Release
5. 上传可执行文件

#### 方法 2: 手动触发

在 GitHub 仓库页面：
1. 进入 "Actions" 标签
2. 选择 "Build and Release" 工作流
3. 点击 "Run workflow"
4. 输入版本号
5. 点击绿色的 "Run workflow" 按钮

### 工作流详情

#### 触发条件
- 推送 `v*` 标签（如 v1.0.0）
- 手动触发 workflow_dispatch

#### 构建环境
- 系统: Windows Latest
- Python: 3.10
- 编译器: Nuitka

#### 输出
- 可执行文件: `LpkUnpackerGUI-v{version}.exe`
- 自动创建 Release 页面
- 自动上传 EXE 文件

### 版本号规范

遵循 [Semantic Versioning](https://semver.org/):
- `MAJOR.MINOR.PATCH`
- 示例: `1.0.0`, `1.2.3`, `2.0.0`

### 常见问题

**Q: 编译失败了怎么办？**
- 查看 Actions 日志找出错误
- 检查 Python 版本是否为 3.10
- 确保 `pixi.toml` 和 `pixi.lock` 中的依赖都正确

**Q: 如何重新编译某个版本？**
- 进入 "Actions" → "Build and Release"
- 选择失败的工作流
- 点击 "Re-run all jobs"

**Q: 本地编译和发布的区别？**
- 本地编译: 使用 `pixi run build`
- GitHub Actions: 自动化编译和发布到 Release
- GitHub Actions 更方便且可以在任何电脑上触发

### 脚本帮助

#### prepare_release.py
自动更新版本号和创建更新日志：

```bash
python scripts/prepare_release.py 1.1.0
```

这会：
1. 更新版本号
2. 创建 CHANGELOG 条目
3. 提示后续步骤
