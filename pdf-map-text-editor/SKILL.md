---
name: "pdf-map-text-editor"
description: "PDF 地图、图集、图件、规划图和报告插图的文字识别、样式匹配与可见文字编辑。用户需要识别图内文字/标注，按指定格式在任意位置添加文字，遮盖后重写任意文字，批量修改地图说明框/图例/标注，或尽量匹配上下文的字体、字号、颜色以获得逼真修改效果时调用。"
---

# PDF 图件文字智能编辑

本技能用于对 PDF 地图、图集、工程图件和报告插图做文字级修改。它不是单一“溃口信息”工具：溃口摘要只是预设模式；通用能力是扫描图中文字线索、按坐标在任意位置写入任意文本、必要时遮盖原文字后重写，并尽量按附近文字匹配字号、颜色和字体风格。

## 核心能力

- 识别 PDF 文字层中的文本、页码、坐标、字体资源名、字号和颜色线索。
- 在任意页、任意坐标添加任意文字。
- 用白色或指定底色矩形遮盖旧文字，再写入新文字，实现“修改”效果。
- 用 `match_near` 从目标位置附近的原文字自动推断字号、颜色和宋体/黑体倾向。
- 批量执行 JSON 指令，适合一次改很多页、很多位置。
- 保留溃口信息预设：自动提取 `溃口宽度 + 圩堤名称 + 堤防等级` 并写入右侧说明框。

## 脚本入口

```bash
pdf-map-text-editor/scripts/pdf_map_text_editor.py
```

依赖：

- Python 3
- `pypdf`
- `reportlab`

Codex bundled Python 通常包含这些包。若当前环境缺失：

```bash
python3 -m pip install pypdf reportlab
```

## 工作流

1. 渲染目标页或查看用户截图，确认要写入/修改的位置。
2. 用 `scan` 提取文字块和样式线索：

```bash
python3 pdf-map-text-editor/scripts/pdf_map_text_editor.py scan "input.pdf" \
  --pages 24 \
  --output /tmp/page24-text-blocks.json
```

3. 选择目标坐标。PDF 坐标原点在左下角，单位是 pt。
4. 对新增文字，用 `add`；对修改旧文字，用 `add --cover ...` 或 `apply` JSON。
5. 渲染修改后的代表页，检查新增文字是否可见、是否压到原图、是否和上下文足够一致。

## 添加一段任意文字

```bash
python3 pdf-map-text-editor/scripts/pdf_map_text_editor.py add "input.pdf" \
  --page 24 \
  --text "高桥圩5级堤防，其溃口宽度为380m。" \
  --x 1008.3 \
  --y 609.5 \
  --match-near 1008.3,626 \
  --output "input_文字编辑.pdf"
```

`--match-near` 会在同页附近找原文字块，自动沿用可推断的字号、颜色和字体倾向。

## 遮盖后重写旧文字

适用于“修改任何文字”。先用渲染图量取旧文字所在矩形，再遮盖并重写：

```bash
python3 pdf-map-text-editor/scripts/pdf_map_text_editor.py add "input.pdf" \
  --page 24 \
  --text "修订后的说明文字" \
  --x 1008.3 \
  --y 609.5 \
  --cover 1006,603,145,12,#FFFFFF \
  --match-near 1008.3,626 \
  --output "input_文字编辑.pdf"
```

如果底色不是纯白，先从渲染图取样，再把 `#FFFFFF` 换成更接近的颜色。复杂底图区域建议小范围多次遮盖，不要一次覆盖过大矩形。

## 批量 JSON 指令

`apply` 支持一次执行多个操作：

```json
[
  {
    "page": 24,
    "text": "新增说明文字",
    "x": 1008.3,
    "y": 609.5,
    "match_near": {"x": 1008.3, "y": 626, "radius": 80}
  },
  {
    "page": 25,
    "text": "替换后的标注",
    "x": 196.5,
    "y": 447.6,
    "cover": {"x": 190, "y": 442, "width": 160, "height": 14, "fill": "#FFFFFF"},
    "font_size": 7.5,
    "color": "#000000"
  }
]
```

运行：

```bash
python3 pdf-map-text-editor/scripts/pdf_map_text_editor.py apply "input.pdf" \
  --operations edits.json \
  --output "input_文字编辑.pdf"
```

操作字段：

- `page`: 1 基 PDF 页码。
- `text`: 要写入的任意文字，支持换行。
- `x`, `y`: 写入基线坐标。
- `match_near`: 可选，附近文字坐标；用于自动匹配字号、颜色、字体倾向。
- `cover`: 可选，遮盖矩形，字段为 `x/y/width/height/fill`。
- `font_size`, `color`: 可选，显式覆盖自动匹配结果。
- `align`: `left`、`center`、`right`。
- `max_width`: 可选，按宽度自动换行。
- `line_spacing`: 可选，默认 1.2。

## 溃口信息预设

对防洪风险图，可直接使用预设模式：

```bash
python3 pdf-map-text-editor/scripts/pdf_map_text_editor.py breach-summary "input.pdf" \
  --output "input_补充溃口信息.pdf" \
  --report "input_溃口信息写回清单.tsv"
```

默认会识别：

- `溃口宽度：380m`
- `高桥圩：5级堤防`

并写入：

- `高桥圩5级堤防，其溃口宽度为380m。`

默认写入坐标适配常见 A3 横向洪水风险图右侧“说明”框：

- `--x 1008.285706`
- `--y 609.5`
- `--font-size 7.47`

## 字体与逼真度策略

- 先用 `scan` 找目标附近的原文字块。
- 优先用 `match_near`，让脚本继承附近文字的字号和颜色。
- 字体资源名若包含 `SimSun`/`Song`，脚本倾向用宋体；若包含 `Hei`/`Sans`，倾向用黑体。
- 很多地图 PDF 的原文字层是隐藏 OCR 层或嵌入子集字体，不能直接复用原字体；脚本会用本机可嵌入中文字体做视觉近似。
- 如果要求更逼真，传入与制图软件一致的中文字体：

```bash
python3 pdf-map-text-editor/scripts/pdf_map_text_editor.py apply "input.pdf" \
  --operations edits.json \
  --font "/path/to/chinese-font.ttf" \
  --output "input_文字编辑.pdf"
```

## 必做校验

- `pdfinfo` 页数与原 PDF 一致。
- 渲染至少 3-5 个代表页，检查新增/修改文字可见。
- 对修改旧字的区域，确认遮盖矩形没有露出旧字，也没有覆盖相邻内容。
- 检查字号、行距、颜色、对齐方式是否接近上下文。
- 对批量任务，保留 JSON 指令和输出清单，方便追溯。
