# 公示清册可见行表重建

从**矢量页面描述**（JSONL/Python 对象）重建与可见纸面一致的业务行表。不读取、
解析或生成 PDF、图片，也不使用 OCR。

实现仅依赖 Python 标准库。

## 快速开始

```bash
# 生成规范夹具、坏夹具和大页册夹具
python3 -I /workspace/roster.py fixture --kind all --out /workspace/generated_fixtures

# 重建规范夹具
python3 -I /workspace/roster.py reconstruct \
  /workspace/generated_fixtures/canonical_book.jsonl \
  --rows-out /workspace/out/rows.jsonl \
  --cells-out /workspace/out/cells.jsonl \
  --diag-out /workspace/out/diagnostics.jsonl \
  --staging /workspace/out/staging.sqlite3 \
  --keep-staging

# 测试
python3 -I /workspace/test_roster_reconstruct.py
```

省略 `--rows-out` 时，业务行 JSONL 写到标准输出；摘要写到 stderr。
`--rows-out rows.csv` 输出宽表 CSV（每列附 `<col>__status`）；
`--cells-out x.csv` 可输出长表 CSV，否则按 JSONL 输出安全单元记录。

## 输入页面描述

推荐 JSONL 流式格式，每行一个记录。图元记录的出现顺序就是绘制顺序；单元记录
不是图元，可放在页内任意位置，但建议先声明全部单元。

```jsonc
{"type":"book_start","book_id":"demo"}
{"type":"page_start","page_id":"P1","page_no":1,"bounds":[0,0,900,620]}

{"type":"cell","cell_id":"r1c1","row_id":"R1","col_id":"name",
 "row_no":1,"col_no":1,"reading_order":10,
 "rect":[20,20,420,90]}

{"type":"word","word_id":"w1","text":"公开","rect":[40,35,120,75]}
{"type":"word","word_id":"w2","text":"清册","rect":[140,35,220,75]}
{"type":"blank","rect":[30,115,280,175]}

{"type":"page_end","page_id":"P1"}
```

也支持单行完整页对象：

```json
{"type":"page","page_id":"P1","bounds":[0,0,900,620],
 "cells":[...],"primitives":[...]}
```

或 Python API：

```python
from roster_reconstruct import process_page_description

result = process_page_description(page_dict)
```

### 坐标

- 所有坐标必须是整数；`bool`、浮点字符串、小数均拒绝。
- 矩形统一为 `[x0, y0, x1, y1)`，半开。
- 正面积相交条件是
  `max(a0,b0) < min(a2,b2)` 且 y 轴同样严格成立。
- 共边、共点的面积为零，不算接触遮挡，也不算单元正面积相交。

## 判定规则

设词框为 `W`，所有后续图元为 `P_j`，则遮挡区域是

```text
Occluded = W ∩ (P_1 ∪ P_2 ∪ ...)
Visible  = W \ Occluded
```

其中词图元和空白框图元都按**整个矩形不透明**处理；词框按描述填满，不做字形
栅格化。

1. `Visible = ∅`：全遮消失；不输出文本、证据或诊断词框。
2. `Occluded = ∅`：完全可见。
   - 必须完整包含在唯一一个单元矩形内，才分配给该单元。
   - 否则所有与词框有正面积相交的单元均为 `undecidable`。
3. 其余为半遮：
   - 只让与 `Visible` 的各矩形碎片有正面积相交的单元成为 `undecidable`。
   - 仅被遮挡部分相交的单元不受该词影响。
4. 可判单元内，按
   `(词框上边 y0, 词框左边 x0, 绘制序号)` 排序，词间恰好一个空格。
5. 没有分配词的可判单元为 `blank`，文本是空字符串。
6. 不可判单元：
   - `status = "undecidable"`；
   - `"text"` 为 `null`（JSON 业务行的 `text_by_col` 同样为 `null`）；
   - 不返回词级证据、被遮词文本、整词摘要或被遮词字框；
   - 只给几何原因码。
7. 同一业务行的其他单元照常输出。`row_id` 相同的单元可分布在不同页，导出时
   合并为一条业务行。

原因码：

- `partial_occlusion_visible_area`
- `fully_visible_word_not_contained_in_one_cell`

## 输出结构

业务行 JSONL 每行：

```json
{
  "record_type": "business_row",
  "row_id": "R1",
  "row_no": 1,
  "page_ids": ["P1", "P2"],
  "text_by_col": {"name": "公开 清册", "addr": null},
  "cells": [
    {
      "cell_id": "r1c1",
      "row_id": "R1",
      "col_id": "name",
      "status": "ok",
      "text": "公开 清册",
      "rect": [20, 20, 420, 90],
      "evidence": [
        {"word_id": "w1", "draw_order": 1, "rect": [40, 35, 120, 75]}
      ]
    },
    {
      "cell_id": "r1c2",
      "status": "undecidable",
      "reasons": [{"code": "partial_occlusion_visible_area", "kind": "geometry"}]
    }
  ]
}
```

状态：

- `ok`：可判且有词；
- `blank`：可判但无词；
- `undecidable`：不可判，不输出文字。

## 错误

以下情况抛出/报告 `PageDescriptionError`，CLI 以退出码 `2` 输出安全 JSON 错误：

- 非整数坐标、非半开正面积矩形、缺字段、未知/坏图层；
- 单元之间存在正面积重叠；
- 同一页或跨页出现重复 `cell_id`，或重复业务键 `(row_id, col_id)`；
- 任意图元或单元越出页边界。

错误只包含页号、JSONL 行号、单元 ID、违规坐标轴等定位/几何信息，不回显原始
词记录或词文本。未预期异常也只返回异常类型，避免原始异常/回溯回显源数据。

## 大页册与内存/性能

- JSONL 逐行读取；一次只处理一页的图元，不把整册 JSON 读入内存。
- CLI 使用 SQLite 暂存**已安全判定**的单元，支持跨页重复单元检查和业务行排序；
  导出使用游标分批读取。默认退出时删除暂存库，`--keep-staging` 可保留审计。
- 不对页面面积栅格化；所有计算是整数矩形相交、包含和矩形差集。
- 每页为单元和图元建立静态二维包围盒层次（BVH）。每个词走空间索引查询候选，
  不重扫整页。工作量与索引剪枝后的候选/可见碎片数量相关；对清册常见的稀疏、
  规则表格接近对数查询。
- 生成器同样逐行写文件，可生成多页、多行、多列和固定随机遮挡的大夹具。

## 隐私边界

- 被全遮或半遮的词不会进入安全单元结果、证据、诊断、CSV、最终 JSONL 或 SQLite
  暂存文本列。
- 不可判单元不保存词级 `evidence`；导出层还会强制把不可判单元的文本/证据置空。
- 不生成掩码词、词长、哈希等“整词摘要”，也不返回被遮词矩形。
- 全遮/半遮词数仅作为页级聚合计数出现在统计中。
- 输入文件本身当然仍含原始描述；本工具不会把其中的被遮文本复制到出库产物。

## 夹具

```bash
# 规范小册：普通词、全遮、新词盖旧词、半遮、跨单元词、跨页行
python3 -I /workspace/roster.py fixture --kind canonical --out /tmp/fx

# 异常夹具
python3 -I /workspace/roster.py fixture --kind bad --out /tmp/fx

# 大页册
python3 -I /workspace/roster.py fixture --kind large --out /tmp/fx \
  --pages 100 --rows 80 --cols 8 --words-per-cell 5
```

测试包含半开边界、矩形差集、随机空间索引对照、所有关键遮挡分支、跨页重复单元、
流式暂存以及“秘密词不出现在输出/诊断/暂存库字节中”的隐私断言。
