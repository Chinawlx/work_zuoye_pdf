# PDF → Markdown 最小转换系统 + 质量检查 Skill

把 PDF 转成 Markdown 和结构化中间结果(blocks / tables),并对扫描页做 OCR、对解析质量做自动检查与人工复核提示。配套一个可复用的质量检查 Skill / SOP。

---

## 1. 如何运行

### 环境准备

```bash
pip install -r requirements.txt
```

主要依赖:`PyMuPDF`(文字层探测/文本块/渲染)、`pdfplumber`(文字页表格)、`requests`(调 OCR 接口)、`python-dotenv`(读 .env)。

### 配置 OCR(扫描页需要)

扫描页用硅基流动 (SiliconFlow) 上的 `PaddlePaddle/PaddleOCR-VL-1.5` 做识别。在项目根目录建 `.env`:

```
SILICONFLOW_API_KEY=sk-你的key
```

`.env` 已被 `.gitignore` 忽略,不会提交。若未配置 key,扫描页会自动降级为 `needs_ocr: true` 标记,文字页照常解析。

### 运行

```bash
# 默认处理 assignment_pdf_markdown_skill_assets/sample_pdf_to_markdown_note.pdf
python convert_pdf.py

# 也可显式指定 PDF
python convert_pdf.py path/to/your.pdf --outdir outputs
```

可选参数:`--dpi`(扫描页渲染精度,默认 200)、`--ocr-model`、`--base-url`。

---

## 2. 输出文件在哪里

全部生成在 `outputs/`:

| 文件 | 内容 |
| --- | --- |
| `document.md` | 带 `<!-- page: N -->` 页码标记的 Markdown,含标题/正文/表格/扫描页标记 |
| `blocks.json` | 逐页文本块,每块带 `type` / `bbox`(source locator) / `is_ocr` |
| `tables.json` | 表格专用结构:表标题/表头/行列/期间列/合计校验/来源页码 |
| `qa_report.md` | 解析质量报告:页码核对、合计校验、问题清单、人工复核清单、优化思路 |

> 说明:作业推荐结构里 `outputs/` 写的是「`blocks.json` 或 `tables.json`」。这里两者都输出——`blocks.json` 存文本块,`tables.json` 单独存表格结构(含行列、表标题、source locator、合计校验),对应作业「加分项」中保留表格行列信息和 source locator 的要求。

---

## 3. 实际用时

> **【请填写】约 X 小时。**(建议如实记录:设计 + 编码 + 调试 OCR 格式 + 写 Skill/README 的总时长)

---

## 4. 使用了哪些 AI 工具,分别做什么

| 用途 | 怎么用 | 是否人工复核 |
| --- | --- | --- |
| 搭主程序脚手架 | 让 AI 生成页面分类、文本块抽取、表格抽取、Markdown 渲染的初版结构 | ✅ 逐函数读过,调整了阈值和分支 |
| OCR 接口对接 | 让 AI 按硅基流动文档写 PaddleOCR-VL 的多模态调用 | ✅ 核对了请求格式(image_url / base64 / temperature) |
| VLM 输出格式兜底 | 针对实际跑出来的三种表格格式(标准 / 缺竖线 / 散装每格一行),让 AI 写归一化逻辑 | ✅ 用真实返回逐一验证 |
| 写质量检查 Skill | 让 AI 按作业 10 点起草,再对齐到代码实际逻辑 | ✅ 核对每条与系统行为一致 |

**核心原则:不盲信 AI 输出。** 所有 AI 生成的代码我都实际运行验证,关键逻辑(合计校验、OCR 数值)单独核对;格式归一化用每次真实的 VLM 返回回归测试,而不是只看代码"看起来对"。

---

## 5. AI 输出中哪些内容我人工复核了

- **OCR 识别的数值**:第 3 页扫描页的 `15.00 / 18.20 / 8.50 / 7.80 / 23.50 / 26.00` 全部对照原 PDF 人工核对——VLM 可能漏行/串列,机器看不出。
- **合计校验逻辑**:手算验证了表 36-1(120.50+88.00+160.75=369.25)和表 37-1(316.00 / 296.50)的明细之和,确认校验代码结果正确。
- **VLM 格式归一**:三种表格格式各用真实返回跑了一遍,确认归一后表格能正确渲染并进 `tables.json`,且重组失败时不臆造结构。
- **页眉页脚剔除**:确认"XX公司…节选""第 N 页"没混入正文。

> **【可补充】** 如果你额外人工复核了别的点,在这里补一句。

---

## 6. 已完成 / 未完成 / 最大风险

### 已完成
- 逐页实测判断文字层(不假设),文字页 PyMuPDF+pdfplumber 解析,扫描页自动调 PaddleOCR-VL。
- Markdown 保留页码标记、标题层级、表格、扫描页标记。
- 结构化中间产物 `blocks.json` / `tables.json`,每块带 page+bbox 的 source locator。
- 合计数自动校验(明细之和 == 合计)。
- VLM 三种不稳定输出格式的归一化 + 扫描页排版。
- 质量报告 + 人工复核清单;OCR 表强制 `NEED_RECHECK`。

### 未完成 / 已知局限
- **跨页表格合并**:本样例无跨页表,未实现按表头连续性合并。
- **数值单位绑定**:表注的"百万元"未绑定到每个 cell。
- **OCR 表的列结构**:VLM 偶尔漏行/串列,散装格式靠启发式重组,复杂表可能失败(失败时保留原文+转人工,不臆造)。
- **OCR 无逐字置信度**:VLM 不返回置信度,只能整体标 NEED_RECHECK。

### 最大风险
**扫描页的关键数值**。VLM OCR 可能把数字认错或漏掉一整行,且没有置信度可供自动判断。系统已对所有 OCR 数值强制 `NEED_RECHECK` 并列入人工复核清单——这类数字在入账/披露前必须人工逐项核对。

---

## 7. 如果多给半天,优先优化什么

1. **OCR 表格结构还原**(收益最高):接版面分析(如 PaddleOCR PP-Structure)做表格区域+单元格识别,减少漏行/串列,降低人工复核量。
2. **跨页表格合并**:按表头一致性自动拼接跨页表格。
3. **PDF↔Excel GT 对账**:实现两源数值比对,不一致自动标 `NEED_RECHECK`,对应作业里的对账场景。
4. **bad case 回归测试集**:把每次踩到的格式/解析坑固化成测试用例,持续防回归。

---

## 8. Git 协作习惯(避免智能体乱改文件)

- **分支策略**:`main` 只保留可运行版本;开发走 `feature/xxx` 分支,如 `feature/ocr-vl`、`feature/table-normalize`。
- **小步 commit**:一次 commit 只做一件事,信息写清「做了什么、为什么」,如 `fix: 归一化 VLM 散装表格(每格一行)`,便于回溯和 review。
- **用 diff 审 AI 改动**:智能体改完先 `git diff` 逐行看,确认只动了该动的文件——尤其防止它顺手改了 `outputs/`、`.env`、Skill 文件。
- **PR review**:改动通过 Pull Request 合入,至少自审一遍 diff;CI 跑通(脚本能跑、产物正常)再合。
- **保护关键文件**:`.gitignore` 忽略 `.env`(密钥不入库);`outputs/` 是生成物,提交前确认是真实运行结果而非占位。
- **不让智能体直接写 main**:所有 AI 生成的改动都经过人工 diff 审核才提交,杜绝「一句话让它批量改文件」导致的不可控变更。

---

## 仓库结构

```text
pdf-md-skill-assignment/
  README.md
  requirements.txt
  convert_pdf.py                       # 主程序
  .env                                 # 本地配置(已 gitignore)
  .env.example                         # 配置模板
  skills/
    pdf_to_markdown_review_skill.md    # 任务二:质量检查 Skill/SOP
  outputs/
    document.md
    blocks.json
    tables.json
    qa_report.md
  assignment_pdf_markdown_skill_assets/
    sample_pdf_to_markdown_note.pdf
```
