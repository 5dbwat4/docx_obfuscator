# docx_obfuscator

把 Word `.docx` 变成「**打开显示正常、复制出来是乱码**」的文档。

核心思路不是加密，而是 **字体子集化 + cmap 重映射 + OOXML 嵌入式字体**：

1. 把正文统一到少量已知字体（宋体 / Times New Roman / Consolas）；
2. 为每一小批字符生成一个只包含这些字符的“假字体”，字体里 cmap 的键是随机码位，值才指向真实字形；
3. 把文档中的每个字符替换成随机码位，并让该字符使用对应的假字体；
4. 将假字体以 `.odttf`（XOR 混淆的 TrueType）嵌入 DOCX。

Word 通过这些内嵌字体正常绘制字形，因此屏幕上看起来和原文完全一样；但复制出来的是随机码位，脱离内嵌 cmap 就没有意义。

---

## 目录结构

```
docx_obfuscator/
├── README.md
├── requirements.txt
├── run.py                       # 命令行入口
├── docx_obfuscator/
│   ├── __init__.py
│   ├── config.py                # 字体族、权重、算法参数
│   ├── ooxml.py                 # ZIP / XML / run 解析等底层工具
│   ├── styles.py                # 段落/字符样式继承与主题字体解析
│   ├── fonts.py                 # 子集字体构建、假字体、odttf XOR
│   ├── normalize.py             # 阶段 1：字体归一化
│   ├── obfuscate.py             # 阶段 2：分桶、cmap 重映射、嵌入字体
│   └── verify.py                # 阶段 3：结构/映射/字形/显示校验
├── docs/
│   ├── DESIGN.md                # 原理、逆向细节、踩坑记录
│   └── example-verify.txt       # 一次完整校验的输出示例
├── tests/
│   ├── test_core.py             # 纯逻辑单元测试
│   └── test_fontbuild.py        # 需要 Windows 系统字体的字体构建测试
└── tools/
    └── word_check.ps1           # 用 Word COM 打开源/输出并对比页数与字体
```

---

## 环境要求

* Windows（需要系统字体 `simsun.ttc`、`times*.ttf`、`consola*.ttf`）；
* Python 3.10+；
* `fonttools`（当前开发环境为 4.53.1）。

```powershell
python -m pip install -r requirements.txt
```

如果字体在别的目录，设置环境变量或传 `--font-dir`：

```powershell
$env:DOCX_OBF_FONTDIR = "D:\fonts"
```

---

## 快速开始

一条命令完成归一化、混淆、校验：

```powershell
python run.py all .\input.docx -o .\out.docx
```

默认会生成：

| 文件 | 说明 |
| --- | --- |
| `out.docx` | 最终混淆文档 |
| `out.docx.normalized.docx` | 字体归一化后的中间文档 |
| `out.docx.buckets.tsv` | 字符 → 码位 → 字体 的完整映射 |
| `out.docx.verify.txt` | 校验报告 |

也可以分步执行：

```powershell
python run.py normalize input.docx -o normalized.docx
python run.py obfuscate normalized.docx -o obfuscated.docx --map buckets.tsv
python run.py verify normalized.docx obfuscated.docx --map buckets.tsv --log verify.txt
```

Python API：

```python
from docx_obfuscator import Options, normalize, obfuscate_docx, verify

opts = Options(seed=20261011, bucket_divisor=10)
normalize("input.docx", "normalized.docx")
obfuscate_docx("normalized.docx", "obfuscated.docx", "buckets.tsv", opts)
ok = verify("normalized.docx", "obfuscated.docx", "buckets.tsv", opts, "verify.txt")
```

常用参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--seed` | `20261011` | 随机种子；同一种子可复现映射（字体 GUID 仍随机） |
| `--bucket-divisor` | `10` | 每 N 个待混淆字符生成一个假字体；越小字体越多、越碎 |
| `--prefix` | `_5dbwat4_` | 假字体名前缀；总长度必须 ≤ 31 |
| `--font-dir` | `C:\Windows\Fonts` | 源字体目录 |
| `--samples` | `60` | 字形轮廓抽样校验数量 |

---

## 处理流程

```
input.docx
   │
   │  normalize.py
   │  ├─ 解析 styles.xml / theme1.xml
   │  ├─ 沿「文档默认 → 段落样式链 → 字符样式链 → run 直接格式」求有效字体
   │  └─ 非 ASCII → 宋体，ASCII → Times New Roman（楷体保留）
   ▼
normalized.docx
   │
   │  obfuscate.py
   │  ├─ 统计每个字体族待混淆字符数 N，分配 N/10 个桶
   │  ├─ 每个桶生成随机字体名 _5dbwat4_xxxxxxxx
   │  ├─ 每个 (字符, 字体族) 分配一个同类随机码位
   │  ├─ 每个桶按 regular/bold/italic/boldItalic 构建假字体
   │  ├─ 字体以 odttf（前 32 字节 XOR 反转 GUID）嵌入
   │  └─ 文本 run 拆成单字符 run；非文本子节点原样保留
   ▼
obfuscated.docx + buckets.tsv
   │
   │  verify.py
   │  ├─ XML 合法、文本部件一致
   │  ├─ 换行/图片/表格/公式/书签/字符样式/媒体字节数不变
   │  ├─ 由映射反向重建「屏幕显示文本」并与源文本逐字比较
   │  ├─ fontTable / rels / Content_Types / settings 完整性
   │  └─ cmap 覆盖 + 随机抽样比较字形轮廓
   ▼
verify.txt
```

### 为什么只混淆字母、数字和汉字？

`obfuscate.py` 仅处理 Unicode 类别 `Lu/Ll/Lt/Lo/Nd`，并且跳过空白。标点保留原码位，原因是：

* Word 的中文标点压缩（`compressPunctuation`）和禁则处理依赖具体标点字符；
* 换行、两端对齐、行高等排版行为更稳定；
* 复制结果仍然是「乱码但可辨认结构」，不影响原文可读性验证。

### 为什么要按字体族分桶？

假字体的 `head/hhea/OS/2` 垂直度量来自该族的 regular 字体。宋体、Times New Roman、Consolas 的行高不同，如果混在一个假字体里，行高会被其中一种字体带偏，页面布局就会变。因此：

* 每个桶只属于一个字体族；
* 每个桶的骨架（skeleton）取该族 regular 字体；
* 粗体/斜体只在确有对应源字体时使用；宋体没有粗体源时用“偏移叠加”模拟伪粗体。

---

## 验证结果

### 本次整理版的实测

在 `lab1_src.docx` 上完整跑通 `normalize -> obfuscate -> verify`，并用 Word COM 做了真实打开对比：

| 指标 | 源文档 | 混淆文档 |
| --- | --- | --- |
| 页数 | 19 | 19 |
| 字数 | 3,548 | 3,548 |
| 字符数 | 6,030 | 6,030 |
| 抽样中的假字体数 | 0 | 49 / 63 |
| 分页结构 | — | 完全一致 |

* 共混淆 4,811 个字符（正文 5,740 字符）；
* 479 个桶、923 个内嵌 face、8,521 行映射；
* 图片 33 个、表格 1 个、域代码、书签、换行数量全部保持一致；
* 输出约 9.6 MB（`bucket_divisor=10`）。

完整校验日志见 `docs/example-verify.txt`。

### 既有实验（历史日志）

仓库中的历史日志记录了两份文档的实测结果：

**实验报告（report）**

* 正文 20,318 字符，其中 14,466 个字符被混淆；
* 1,445 个假字体、2,127 个字体 face、19,378 行映射；
* 结构保持：`w:br` 154、`w:drawing` 29、`w:tbl` 4、`m:oMath` 70、公式文本 390 个节点、38 个媒体部件字节完全一致；
* 显示文本反向重建与源文本完全一致；
* 使用 Word 导出 PDF 后逐页比对：页数 34 → 35（分页受嵌入字体影响可能变化），正文文本长度一致。

**实验一（lab1）**

* 574 个假字体，最终 DOCX 约 13 MB；
* `verify_report.py` 的全套结构、映射、cmap 覆盖检查通过；
* 曾出现 1 个 Consolas `f` 的字形抽样误报（字体内部 cmap 与轮廓重放差异），不影响显示。

> 校验脚本会把抽样失败也计为 FAIL；如果只想跑结构性校验，可把 `--samples` 设为 0。

---

## 与旧脚本的对应关系

原始实验目录中的脚本已整理为下面的模块；旧文件仍保留在原处，作为历史记录。

| 旧脚本 | 新位置 |
| --- | --- |
| `normalizer.py` | `docx_obfuscator/normalize.py` |
| `build2.py` | `docx_obfuscator/obfuscate.py` |
| `bucketfont.py` + `fakefont.py` | `docx_obfuscator/fonts.py` |
| `verify_report.py` | `docx_obfuscator/verify.py` |
| `style_fonts.py` / `theme.txt` 中的解析逻辑 | `docx_obfuscator/styles.py` |
| `word_check.ps1` | `tools/word_check.ps1` |
| `build.py`（v1 整字体方案） | 未收录，设计说明见 `docs/DESIGN.md` 第 7 节 |

---

## 已知限制

1. **依赖源字体**：必须能读到 `simsun.ttc` / `times*.ttf` / `consola*.ttf`，否则无法生成字形。
2. **只对 Word/WPS 等支持 OOXML 内嵌字体的阅读器有效**。某些预览器、旧版软件或纯文本提取工具会忽略嵌入式字体，显示为方框或乱码。
3. **复制到纯文本一定得到乱码**，但截图、OCR、PDF 打印仍可还原内容；这属于“提高复制成本”，不是强加密。
4. **文件显著变大**：每个桶最多 4 个 face，每个 face 都带一份完整字体头部；`bucket_divisor` 越小文件越大。
5. **字体名长度**：Word 对字体名有 31 字符限制；`prefix + 8` 的命名必须保持 ≤ 31。
6. **公式、图片、域代码、书签不混淆**：它们不是普通文本 run，混淆会破坏结构。
7. **分页可能变化**：即使行高一致，嵌入字体的字形宽度/度量细节仍可能让个别行发生重排；对分页敏感的文档需要人工复核。

---

## 开发与测试

```powershell
python -m unittest discover -s tests -v
```

`test_fontbuild.py` 在没有 Windows 字体的环境下会自动跳过。

Windows 上可以用 Word COM 做一次真实打开测试（统计页数、字数、假字体抽样数，可选导出 PDF）：

```powershell
.\tools\word_check.ps1 -Source .\normalized.docx -Obfuscated .\obfuscated.docx
```

`docs/DESIGN.md` 记录了 OOXML 内嵌字体的格式细节、odttf XOR 规则、Word 字体回退行为以及本次实现中踩过的坑。
