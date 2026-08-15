# translating-papers

> 一个 Claude Code Skill：把英文学术论文 PDF 完整翻译成中文——**图和正文一起交付**，同时输出 Markdown 和 PDF。

**English**: A Claude Code skill that translates English academic papers into Chinese. It extracts *figures* as well as text, cross-checks that no figure was silently dropped, and renders the result to both Markdown and PDF — no LaTeX required.

---

> ### 🫠 先自曝一下
>
> **这是个纯 vibe coding 产物。** 需求是我提的，坑是我踩的，代码基本是 Claude 写的——我本人是编程小白，看不太懂里面的正则。所以别拿工程规范要求它，能解决问题就行。
>
> **我不维护，也不处理 issue。** 用着不顺手就直接 fork 改成你喜欢的样子，代码 MIT 协议随便改随便发。
>
> 不过该测的都测过了，不是随手生成完就扔上来的：
> - 真实的 24 页期刊论文全流程跑通
> - 故意制造漏图场景，确认告警真的会拦下来（而不是个摆设）
> - 矢量图表、正文内嵌图两种边缘场景各自构造 PDF 验证
> - 提示词检测 8 条正例全中、6 条反例全部正确排除
>
> 开发过程中还测出两个真实 bug 并修掉了：中文提示词因 Windows 编码问题全部漏检；正则的 `\b` 词边界在中文语境下失效。**没测就不敢说能用**——这条底线还是守住了。

---

## 为什么需要它

让 AI 翻译论文 PDF，最常见的失败不是翻错，而是**图没了**。

提取 PDF 文本层是个「安静成功」的操作：零报错、零缺页、正文一字不落，但图一张都不在。你拿到一份读起来通顺、看起来完整的译文，直到发现「如图 3 所示」后面什么都没有。

论文里的图有三种形态，**纯文本提取全都拿不到**：

| 形态 | 为什么会漏 |
|---|---|
| 独立整页图（图注在正文，图在末尾整页） | 该页文字量≈0，但整篇不是扫描件，「扫描件检测」直接放行 |
| 正文页内嵌图 | 该页文字量正常，从任何指标都看不出异常 |
| 矢量绘制的图表 | `get_images()` 返回 0 张，栅格图检测完全失效 |

这个 skill 三种都检测，并做**图数量交叉校验**：正文引用了 Fig. 1–4，就必须产出 4 张图；对不上直接以退出码 3 中止，拒绝在漏图状态下开始翻译。

> 本项目的起因是一次真实事故：一篇 24 页的期刊论文，正文 20 页有文本层，图 1–4 单独占最后 4 页。文本提取一切正常，译文交付了，图一张没有。

---

## 依赖

| 用途 | 依赖 | 安装 |
|---|---|---|
| PDF 解析与渲染 | PyMuPDF | `pip install pymupdf` |
| Markdown → HTML | pandoc ≥ 3.0 | [pandoc.org/installing](https://pandoc.org/installing.html) |
| HTML → PDF | Chrome 或 Edge | 大多数系统已自带 |

**不需要 LaTeX**，也**不需要 OCR 引擎**（文字型 PDF 直接读文本层，扫描件渲染成图后交给模型视觉识别）。

只翻译、不导出 PDF 的话，pandoc 和浏览器可以不装。

---

## 安装

Skill 目录结构与仓库根目录一致，直接 clone 到 skills 目录即可：

**macOS / Linux**

```bash
git clone https://github.com/obenic/translating-papers.git \
  ~/.claude/skills/translating-papers
pip install pymupdf
```

**Windows (PowerShell)**

```powershell
git clone https://github.com/obenic/translating-papers.git `
  "$env:USERPROFILE\.claude\skills\translating-papers"
pip install pymupdf
```

装在 `~/.claude/skills/` 下是**全局生效**（任何目录都能用）；只想在某个项目里用就放到该项目的 `.claude/skills/` 下。

装好后新开一个 Claude Code 会话，说「翻译这篇文献」即可。

---

## 使用

把 PDF 路径告诉 Claude 就行：

```
翻译桌面上的 example-paper.pdf
```

Claude 会自动完成：提取文本 → 检测并渲染图 → 交叉校验 → 分批翻译 → 写 Markdown → 转 PDF → 清理临时文件。

产物：

```
<原文标题> 中文翻译.md      # 依赖同级图片文件夹
<原文标题> 中文翻译.pdf     # 图片已内嵌，可单独发送
<原文标题> 中文翻译_figs/   # 图片
```

### 翻译规范

- 全文翻译，不跳段（摘要、引言、结果、讨论、方法、致谢、图注）
- **参考文献保留英文原文**，不翻译
- 术语首次出现附原文：系间窜越（intersystem crossing, ISC）
- 化学式、单位、数值、公式编号、图表编号原样保留
- 人名、期刊名保留英文

---

## 自动触发（可选）

Skill 默认由模型读 `description` 判断是否调用——大多数时候没问题，但措辞边缘可能漏。想要**确定性触发**，加一个 `UserPromptSubmit` hook：

编辑 `~/.claude/settings.json`：

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python \"<你的home>/.claude/skills/translating-papers/hook_detect.py\"",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

> 路径请填绝对路径。已有其他配置的话，把 `hooks` 键**合并**进去，不要覆盖整个文件。

`hook_detect.py` 要求**动作词 + 对象词同时出现**才触发，避免误伤：

| 触发 | 不触发 |
|---|---|
| 翻译这篇文献 | 翻译这段代码注释 |
| 把这个 PDF 翻译成中文 | 这篇 paper 讲了什么 |
| translate this paper | 总结一下这篇论文 |
| 桌面那个英文文献翻译成中文 | 把变量名翻译成英文 |

改完需要重启 Claude Code，或打开一次 `/hooks` 让配置重新加载。

---

## 脚本说明

三个脚本都可以脱离 Claude 单独当命令行工具用。

### `extract_paper.py` — 提取文本 + 图

```bash
python extract_paper.py <pdf> [-o OUTDIR] [--dpi 200] [--max-width 1600] [--pages 21-24]
```

产出 `text.txt`、`figures/pNN.png`、`manifest.json`。

| 退出码 | 含义 |
|---|---|
| `0` | 图数量与正文引用一致 |
| `3` | **图可能漏了** — 查 `manifest.json` 的 `per_page`，用 `--pages` 强制渲染 |
| `1` | 出错（通常是缺 PyMuPDF） |

输出示例：

```
pages       : 24  (text pages: 20)
figures     : 4 rendered -> ./figures
  p 21  figures/p21.png     1600x1533   787KB  (standalone figure page; raster image 98% of page)
  p 22  figures/p22.png     1600x1535  1192KB  (standalone figure page; raster image 93% of page)
  p 23  figures/p23.png     1600x1357  1620KB  (standalone figure page; raster image 98% of page)
  p 24  figures/p24.png     1600x1891  1988KB  (standalone figure page; raster image 94% of page)
referenced  : Fig [1, 2, 3, 4]

OK: figure count consistent with text references.
```

### `md_to_pdf.py` — Markdown 转 PDF

```bash
python md_to_pdf.py <input.md> [-o out.pdf] [--font serif|sans] [--keep-html]
```

pandoc → 自包含 HTML（图片转 data URI）→ Chrome/Edge 无头打印。中英文混排字体栈把拉丁字体排在前面，CJK 靠逐字符回退，避免中文字体渲染出难看的拉丁字形。

### `hook_detect.py` — 提示词检测

读 stdin 的 hook JSON，命中则输出注入指令。异常输入一律静默退出 0，不会卡住你的对话。

---

## 工作原理

图检测对每一页取三个指标——文字量、栅格图面积占比、矢量绘制操作数——任一命中即渲染该页：

```python
if 文字量 < 200 and (图面积占比 > 1% or 绘制操作数 >= 10):  # 独立整页图
if 图面积占比 >= 5%:                                        # 正文内嵌图
if 绘制操作数 >= 50:                                        # 矢量图表
```

交叉校验扫描正文里的图号引用（`Fig. 3` / `Figure 3` / `Fig. 3 |`），排除 `Supplementary` 和 `Extended Data`（补充材料是独立文件，正文引用 SI 图 17 不代表正文有 17 张图），取最大值作为预期图数。

Markdown 输出用 pandoc 的 `implicit_figures`，把图和图注编译成 `<figure>` 原子块：

```markdown
### 图 1 | 标题
![**图 1 | 完整图注。** **a** …… **b** ……](figs/p21.png)
```

**图注必须写在 `![...]` 方括号里。** 写成独立段落的话，分页时图片会被推到下一页顶部，紧跟着的是下一张图的图注——读者看到「图 1 的图片 + 图 2 的图注」。这种错误在 Markdown 里肉眼完全正常，只在 PDF 里暴露。

---

## 已知限制

- 扫描件（无文本层）依赖模型视觉识别，准确率取决于扫描质量
- 图注错位、章节遗漏这类问题脚本查不出来，需要人工抽查生成的 PDF
- 交叉校验是启发式的：一页可能含多图，也可能一图跨页，数量不符时是**提示复查**而非断言出错
- 中英文字体依赖系统已装的 Han 字体，缺失时回退到系统默认
- 主要在 Windows 11 + Python 3.13 上验证；macOS / Linux 路径已适配但未实机测试

---

## 版权提示

翻译他人论文属于产生**演绎作品**。很多开放获取论文用的是 CC BY-NC-ND 协议，其中 **ND（禁止演绎）** 条款明确不允许公开分发翻译版本。自己阅读学习没问题，公开发布译文前请先确认原文许可条款。

---

## License

MIT — 见 [LICENSE](LICENSE)。
