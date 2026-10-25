# 贡献指南

感谢你愿意改进划词翻译。

## 提交 Issue

请尽量包含：

- Windows 版本（例如 Windows 11 24H2 x64）；
- 程序版本（设置窗口底部或 `运行日志.log` 开头）；
- 复现步骤；
- 实际表现和预期表现；
- 相关日志片段（**请先删除生词、翻译内容和个人路径**）。

## 提交 Pull Request

1. Fork 仓库并从 `main` 创建分支；
2. 保持改动聚焦，不要顺便格式化整个项目；
3. 运行基础检查：

   ```powershell
   python -m py_compile 划词翻译.pyw winutil.py trans_core.py vocab_store.py
   ```

4. 如果改动了 UI，请附上截图或 GIF；
5. 如果改动了翻译接口，请说明降级路径和失败表现；
6. 不要提交 `生词本.json`、`翻译缓存.json`、`设置.json`、日志、exe 或构建目录。

## 代码风格

- Python 3.10+；
- 尽量只使用标准库；
- UI 文案使用中文；
- 新增 Win32 调用时补齐 `argtypes` / `restype`，避免 64 位指针截断；
- 耗时操作不要阻塞 Tk 主线程。

## 许可

提交 PR 即表示你同意以本项目的 MIT License 发布你的贡献。
