# 审稿小工具

本地论文审稿辅助应用。当前第一版实现“附图”标签页：

- 输入 PDF、DOC、DOCX。
- 自动识别 `Figure/Fig.` 图注。
- 渲染并裁切/拼接附图，点击可查看清晰大图。
- 展示英文图注、图注中文翻译、相关正文段落和段落中文翻译。
- “摘要”标签页会生成一句话概括、研究背景、方法与数据、关键发现、主要结论。
- “创新点”标签页会生成 3-6 条创新点，并给出证据、意义和可信度。
- “文献核查”标签页会基于创新点检索 Zotero 本地库，判断已有文献是否报道过类似机制/结论。

## 启动

```bash
/Users/junchai/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 /Users/junchai/Documents/审稿/reviewer_tool/server.py
```

然后打开：

```text
http://127.0.0.1:8765/
```

如果想打开最近一次解析结果：

```text
http://127.0.0.1:8765/?latest=1
```

## 依赖

- `soffice`：用于 Word 转 PDF。
- `pdftotext`、`pdfinfo`、`pdftoppm`：用于 PDF 文本抽取和页面渲染。
- Python 包：`Pillow`、`python-docx`、`argostranslate`。
- Zotero 文献库默认路径：`/Users/junchai/Zotero/storage`。

翻译目前使用本地离线模型，适合快速审稿预览；后续可以替换为更高质量的翻译接口。
默认会调用 Codex 模型 `gpt-5.4` 生成高质量翻译、摘要和创新点；如果需要离线回退模式，可这样启动：

```bash
REVIEWER_TOOL_USE_MODEL=0 /Users/junchai/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 /Users/junchai/Documents/审稿/reviewer_tool/server.py
```

如需更换模型：

```bash
REVIEWER_TOOL_MODEL=gpt-5.4-mini /Users/junchai/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 /Users/junchai/Documents/审稿/reviewer_tool/server.py
```

如需更换 Zotero 文献库路径：

```bash
ZOTERO_STORAGE=/path/to/Zotero/storage /Users/junchai/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 /Users/junchai/Documents/审稿/reviewer_tool/server.py
```

首次运行“文献核查”会为 Zotero PDF 建立文本缓存：

```text
/Users/junchai/Documents/审稿/reviewer_tool/work/zotero_index.json
```

后续会根据文件大小和修改时间增量更新。
