# Prebuilt wheels（可选）

将与本环境 **Python 版本、操作系统** 匹配的 `vates_core-*.whl` 放在此目录下，便于：

- 离线或受限环境中由根目录 `install.py` 自动 `pip install`；
- 随 **ComfyUI 自定义节点** 分发预编译扩展（例如 CI 产物或团队内部分享的 wheel）。

**示例：** Linux + CPython 3.11 → `vates_core-0.1.1-cp311-cp311-manylinux_*.whl`

不要将 wheel 提交到公开仓库若涉及内部构建机密；可用 CI Artifact 分发。
