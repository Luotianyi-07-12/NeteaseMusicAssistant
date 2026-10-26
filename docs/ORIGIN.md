# 项目来源与许可范围

本项目的功能研究参考了 [cwuom/netease-cloud-LV10](https://github.com/cwuom/netease-cloud-LV10)，研究时的上游快照为 `0e85d749e63a433a13d6db711967599f843ac2eb`。

本仓库公开的 `ncm_tools/`、`app.py`、测试和构建配置为本次重新实现的应用代码，包括图形界面、Edge 扫码流程、接口兼容处理、轮次与并行管理、统计核验和本地状态管理。

截至 2026-10-25，上述上游快照未包含明确的许可证文件。因此，本仓库使用来源链接署名，公开内容不包含该快照的原始 Python 脚本、图片、README 副本或 Git 历史。项目 MIT 许可证适用于本仓库新写的应用代码，不为上游原作或第三方组件重新授予许可。

本地 Node API 来自 [NeteaseCloudMusicApiEnhanced/api-enhanced](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced)，通过 npm 依赖和锁文件安装。第三方许可仍按各组件原有许可处理，见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。
