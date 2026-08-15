---
name: translating-papers
description: Use when the user asks to translate an academic paper, PDF, or foreign-language literature (e.g. "翻译这篇文献", "把PDF翻译成中文", "translate this paper", 上传英文文献要求翻译), including scanned PDFs and papers whose figures sit on separate pages.
---

# 文献翻译

把学术论文 PDF 译成中文，**图文完整**，同时产出 Markdown 与 PDF。

## 铁律：论文 = 正文 + 图

只提取文本层，你会交付一份**看起来完整、实际残缺**的译文——图注翻译得再好，读者也看不到图。

这不是假设，是本 skill 的成因：一篇 24 页论文，正文 20 页有文本层，图 1–4 单独占第 21–24 页。文本提取一切正常、零报错、零缺页，图却一张都没进译文。

PDF 里的图有三种形态，文本提取全都拿不到：

| 形态 | 为什么会漏 |
|------|-----------|
| 独立整页图 | 该页文字量≈0，但整篇不是扫描件，"扫描件检测"放行 |
| 正文页内嵌图 | 该页文字量正常，看不出异常 |
| 矢量绘制图表 | `get_images()` 返回 0 张，栅格图检测完全失效 |

`extract_paper.py` 三种都检测，并做**图数量交叉校验**：正文引用了 Fig.1–4，就必须产出 4 张图。**对不上时脚本 exit 3 并告警——不要在告警未解决前开始翻译。**

## 流程

### 1. 提取

脚本在 skill 目录下（全局安装）。Bash 用 `~`，PowerShell 用 `$env:USERPROFILE`：

```bash
SK=~/.claude/skills/translating-papers
python "$SK/extract_paper.py" "<pdf>" -o "<tmp_dir>"
```

产出 `text.txt`、`figures/pNN.png`、`manifest.json`，并打印摘要。

**先看退出码：**
- `0` — 图数量一致，继续
- `3` — **图可能漏了**。查 `manifest.json` 的 `per_page` 定位缺失页，用 `--pages 5,12-14` 强制渲染，直到一致
- `1` — 报错（缺 PyMuPDF → `pip install pymupdf`）

摘要里 `SCANNED` 表示全篇无文本层：此时 `figures/` 就是全部页面，用 Read 工具逐张视觉识别后翻译。

### 2. 翻译

长文分批，每批 8–10 页，译完追加写入，避免上下文溢出。

- 全文翻译，不跳段：摘要、引言、结果、讨论、方法、致谢、作者贡献、图注
- **参考文献列表保留英文**，不翻译
- 术语首次出现附原文：系间窜越（intersystem crossing, ISC）
- 化学式、单位、数值、公式编号、图表编号（Fig. 1a）原样保留
- 公式用 Unicode 符号表达，复杂公式辅以文字说明
- 人名、期刊名保留英文
- **转义作者行里的 `*` 和 `#`**（通讯作者/共一标记）：写成 `\*` `\#`。两个裸 `*` 会配对成斜体，把中间所有作者名变成斜体

### 3. 写 Markdown

`<原文标题> 中文翻译.md` + 同目录 `<同名>_figs/` 图片文件夹（相对路径，两者必须同级）。

**图注必须写在 `![...]` 方括号内，不能作为独立段落放在图片前后：**

```markdown
## 图

### 图 1 | <中文图注标题>

![**图 1 | 完整中文图注标题。** **a** ……**b** ……（整段图注写在这里）](xxx_figs/p21.png)

### 图 2 | ……

![**图 2 | ……** **a** ……](xxx_figs/p22.png)
```

pandoc 的 `implicit_figures` 会把它变成 `<figure>` + `<figcaption>` 原子块，分页时图和图注永不分离。

**图注写成独立段落会出事**：分页时图片被推到下一页顶部，紧跟着的是**下一张图的图注**，读者看到的是「图 1 的图片 + 图 2 的图注」。这种错误比没有图注更糟，而且肉眼看 Markdown 完全正常，只在 PDF 里暴露。

不要把图和图注拆成"数据图"和"图注"两个章节。

### 4. 转 PDF

```bash
python "$SK/md_to_pdf.py" "<译文.md>"
```

pandoc → 自包含 HTML（图片转 data URI）→ Chrome/Edge 无头打印。不需要 LaTeX。
默认输出同名 `.pdf`；`--font sans` 可换黑体正文，`--keep-html` 保留中间 HTML 排查排版。

图片已内嵌进 PDF，所以 PDF 单独发送不会裂图；Markdown 仍依赖同级图片文件夹。

### 5. 完成前自检

声称完成前逐条确认，不能凭印象：

- [ ] 提取脚本退出码 0（或告警已查证解决）
- [ ] md 里 `![` 数量 == 正文引用的图数量
- [ ] **PDF 里逐张图确认「图 N 的图片」配「图 N 的图注」**——渲染几页出来看，不要只看页数
- [ ] 各章节齐全（对照 text.txt，无整段遗漏）
- [ ] 临时目录已删除

校验图注是否错位：

```bash
python -c "import fitz,re; d=fitz.open(r'<pdf>'); [print(i+1, re.findall(r'图\s*\d+\s*\|', p.get_text())) for i,p in enumerate(d) if p.get_images()]"
```

每个有图的页面应当只出现**一个**图号。

## 常见错误

| 问题 | 解决 |
|------|------|
| 图数量告警，但确实只有 N 张图 | 一页可能含多图。查 manifest 确认后按实际情况继续 |
| 引用了 Supplementary Fig.17 却没这张图 | 正常，SI 是独立文件；脚本已排除 Supplementary/Extended Data |
| PDF 里作者名大段变斜体 | 作者行的 `*` 未转义，改 `\*` |
| PDF 顶部标题出现两次 | 正常已由 CSS 屏蔽；若仍出现，检查 md 是否自带 H1 |
| 拉丁字母显示成打字机风格 | 字体栈把中文字体排在了前面，拉丁字符应先落到 Georgia |
| 图片文件过大 | `--max-width 1200`（默认 1600px） |
| 扫描件字迹模糊 | `--dpi 300` |
| 提取文本断行严重 | 用 Read 工具直接读 PDF（`pages` 参数）交叉核对 |
