# 设计说明

本文记录 `docx_obfuscator` 的实现原理、关键格式细节和实验过程中确认过的行为。

## 1. 威胁模型

目标不是“阻止阅读”，而是“阻止低成本复制”：

* 屏幕显示 / 打印效果与原文一致；
* 在 Word 中选中文本、复制到记事本，得到随机乱码；
* 直接解压 DOCX 读取 `word/document.xml`，得到的也是乱码；
* 攻击者需要提取并还原内嵌字体的 cmap 才能恢复原文。

这属于提高复制成本，不是密码学意义上的加密。

## 2. OOXML 内嵌字体格式

Word 的嵌入式字体使用 `application/vnd.openxmlformats-officedocument.obfuscatedFont`
（`.odttf`）。一个可用的内嵌字体需要同时满足：

1. `word/fontTable.xml` 中存在 `<w:font w:name="...">`；
2. 该字体下存在 `<w:embedRegular r:id="rIdN" w:fontKey="{GUID}"/>` 等条目；
3. `word/_rels/fontTable.xml.rels` 把 `rIdN` 指向 `fonts/fontN.odttf`；
4. `[Content_Types].xml` 为每个 `.odttf` 声明 `obfuscatedFont` 类型；
5. `word/settings.xml` 中存在 `<w:embedTrueTypeFonts/>`（以及可选的
   `<w:saveSubsetFonts/>`）；
6. `word/document.xml` 的 `w:rFonts` 实际引用该字体名。

### 2.1 odttf 混淆算法

`.odttf` 不是加密，只是对字体文件前 32 字节做 XOR：

```text
key = GUID 去掉 {} 和 -，按字节反转（16 字节）
for i in 0..31:
    data[i] ^= key[i % 16]
```

例如 GUID `{EBD39F6F-509E-43B4-881E-2E2575EF0D12}`：

```text
hex = EBD39F6F509E43B4881E2E2575EF0D12
key = 120DEF75252E1E88B4439E506F9FD3EB
```

本项目的 `fonts.obfuscate()` / `deobfuscate()` 使用同一算法，因此可以双向复用。
`w:fontKey` 中的 GUID 必须与混淆时使用的 GUID 完全一致，Word 才能还原字体。

> 注意：GUID 的“标准小端布局”不是这里使用的规则。实际 OOXML 实现使用的是
> “十六进制字节串整体反转”。`reverse.py` 的历史实验验证了这一点。

## 3. 假字体的构建

### 3.1 为什么不用原字体直接改 cmap？

直接把整个宋体 / Times / Consolas 的 cmap 打乱会带来两个问题：

* 文件巨大（宋体完整字体通常十几 MB，嵌入四份就不可接受）；
* 每个文档只需要几百个字符，嵌入完整字体没有必要。

因此每个桶只保留约 10 个字符的字形。

### 3.2 骨架 + 移植字形

`fonts.FontLibrary.build_face()` 的步骤：

1. 取该字体族的 regular 字体，用 `fontTools.subset` 只保留空格，得到“骨架”；
2. 删除 `hdmx/LTSH/VDMX/meta/kern/vmtx/vhea/VORG` 等与单字形无关且可能带来
   兼容问题的表；
3. 对每个 `(原字符, 映射码位)`：
   * 从对应字重的源字体中取该字符的 glyph；
   * 用 `DecomposingRecordingPen` 展开复合字形；
   * 按 `upem` 比例缩放到骨架的坐标系；
   * 用 `TTGlyphPen` 重新生成简单字形；
   * 把新 glyph 写入 `glyf` / `hmtx`，并登记到 glyph order；
4. 新建 cmap，键是**映射码位**，值是新 glyph 名；
5. 修改 `name` 表，使字体族名、PostScript 名与 Word 中引用的假名字一致；
6. 设置 `OS/2.fsType = 0`（允许嵌入），并把 Unicode/CodePage 覆盖范围全部置为
   `0xFFFFFFFF`，避免 Word 认为字体缺字而回退到系统字体；
7. 设置 `usWeightClass`、`fsSelection`、`head.macStyle`，让 bold/italic 生效。

骨架来自 regular 字体，意味着假字体的 `head/hhea/OS/2` 垂直度量与原字体一致，
这是保持行高和页面布局的关键。

### 3.3 粗体与斜体

* Times New Roman / Consolas 有真实的 bold/italic/boldItalic 文件，直接使用；
* 宋体只有 regular 字形：
  * bold 用“同一轮廓向右下偏移 1.8% em 叠加三次”模拟伪粗体；
  * italic 不伪造字形，交给 Word 的合成斜体。

每个字重的字体共享同一套映射码位（同一桶内 `(字符, 字体族) -> 码位` 保持一致），
这样 run 在 regular/bold/italic 之间切换时不会出现码位不一致。

### 3.4 字体命名

```text
_5dbwat4_ + 8 个 urlsafe-base64 字符 = 17 字符
```

* 前缀固定、随机后缀保证不与已安装字体冲突；
* Word 对字体名有 31 字符限制，17 字符留有余量；
* 同一命名同时用于 `fontTable.xml` 的 `w:font w:name`、`w:rFonts` 和字符样式。

## 4. 字体归一化（normalize）

混淆器只认识三个字体族：宋体、Times New Roman、Consolas。原始文档可能包含
Aptos、Segoe UI、Courier New、Noto Sans CJK SC、Liberation Sans 等大量字体，
因此先做一次归一化。

有效字体按以下优先级解析：

```text
docDefaults
  -> 默认段落样式链
  -> 当前段落样式链（basedOn）
  -> 字符样式链（w:rStyle）
  -> run 直接格式（w:rFonts）
```

主题字体（`w:asciiTheme`、`w:eastAsiaTheme` 等）通过
`word/theme/theme1.xml` 的 `majorFont/minorFont` 解析，`script="Hans"` 作为中文
字体的兜底。

归一化规则：

| 字符类型 | 规则 |
| --- | --- |
| ASCII | 若字体在 `{楷体, Times New Roman, Consolas, 宋体}` 中保留，否则改为 Times New Roman |
| 非 ASCII | 若字体在 `{楷体, 宋体}` 中保留，否则改为 宋体 |
| 非 ASCII 且当前为 Times New Roman | 改为 宋体 |
| cs（复杂文种）槽位 | 保持不变 |

楷体被有意保留且不参与混淆，用于需要保留原样的少量文本。校验时这类字符会被
统计为 “left”，但不会被当作错误。

## 5. 分桶与 cmap 重映射

### 5.1 分桶规则

设某字体族的待混淆字符实例数为 N：

```text
bucket_count = max(1, N // bucket_divisor)     # 默认 divisor = 10
```

每个字符实例被随机分配到一个桶。桶与字体族绑定，因此每个假字体只服务一种
行高/字宽体系。

### 5.2 码位选择

每个桶维护一个 `(字符, 字体族) -> 码位` 的映射，满足：

* 同一桶内不同字符不会映射到同一码位；
* 映射码位尽量与原文同类（大写字母 -> 大写字母、汉字 -> 汉字、数字 -> 数字），
  保证字形宽度和断行行为接近；
* 不会映射回自身（池长度 > 1 时）；
* 同一桶的 regular/bold/italic 共用同一码位。

码位池按字符类别划分：`cjk`、`upper/lower`、`digit`、`ea_open/ea_close`、
`fw_*`、`punct`、`math`、`quote` 等。汉字池来自 CJK Unified Ideographs
（U+4E00–U+9FA5）；拉丁字母池允许使用全角字母作为后备。

### 5.3 run 拆分

混淆后每个字符都需要独立的 `w:rFonts`，因此文本 run 被拆成单字符 run：

```xml
<w:r><w:rPr>...</w:rPr><w:t>原</w:t></w:r>
```

变成：

```xml
<w:r><w:rPr>...<w:rFonts w:ascii="_5dbwat4_xxx" .../></w:rPr><w:t>码</w:t></w:r>
```

拆分时的约束：

* 保留原 `w:rPr` 的其余内容（加粗、颜色、下划线、`w:rStyle` 等）；
* 原 `w:rFonts` 的 `w:hint` 保留；
* 非文本子节点（`w:br`、`w:tab`、`w:drawing`、`w:fldChar` 等）原样拆成独立 run；
* 不混淆的字符（空白、标点、楷体）保留原码位与原 `rPr`；
* 不处理 `m:t`（公式），避免破坏 MathML。

## 6. 校验方法

`verify.py` 做三层校验：

### 6.1 结构层

统计源文档与输出文档中以下标签数量并比较：

`w:br`、`w:drawing`、`wp:inline`、`w:bookmarkStart`、`w:tbl`、`w:sectPr`、
`m:oMath`、`m:oMathPara`、`mc:AlternateContent`、`w:instrText`、`w:fldChar`。

同时比较公式文本、字符样式 id 集合、媒体文件字节。

### 6.2 显示层

用 `buckets.tsv` 建立 `(字体名, 码位) -> 原字符` 的反向映射。遍历输出文档的每个
字符：

* 如果它引用了假字体且在映射中，取回原字符；
* 否则保留原字符。

重建出的字符串必须与归一化源文档的全文逐字相等。

### 6.3 字形层

从每个假字体中按映射取 glyph，与源字体对应字符的轮廓比较：

* 对每个 face 的 cmap 覆盖做全量检查；
* 随机抽取若干 `(字体, 字重, 字符)`，用 `DecomposingRecordingPen` 展开后比较
  轮廓点列；
* 宋体 bold 需要按伪粗体规则叠加偏移后再比较。

历史实验中出现过一次 Consolas `f` 的抽样误报，属于轮廓重放细节差异；不影响
Word 显示。可以把 `--samples 0` 跳过该层。

## 7. 版本演进

### v1：整字体打乱

第一版（`build.py` / `fakefont.py`）只生成 3 个假字体（宋体、Times、Consolas），
把整份字体子集化后用一张全局映射表打乱 cmap：

* 优点：实现简单；
* 缺点：字体名固定、映射全局一致，攻击者拿到一份文档后更容易反推；文件仍然较大
  （整份字符集）。

### v2：分桶 + 每桶独立 cmap

第二版（`build2.py` / `bucketfont.py`）把字符随机分桶，每桶独立字体、独立映射：

* 同一个汉字在不同位置可能对应不同码位；
* 每个假字体只含约 10 个字形，文件结构更“碎”；
* 字体族隔离保证行高一致；
* 字符样式与假字体同名，方便 Word 识别和人工排查。

当前目录中的代码即 v2 的整理版本。

## 8. 实现细节与踩坑

1. **XML 不能用 DOM 随意重写**：Word/WPS 生成的文档有大量命名空间和扩展元素，
   用正则处理 `w:r` / `w:t` / `w:rFonts` 更能保持原样。
2. **`w:rPr` 可能有自闭合形式**：`<w:rPr/>` 需要特殊处理；插入 `w:rFonts` 时
   要放在 `w:rStyle` 之后、其他属性之前。
3. **主题字体要先解析**：只读取 `w:rFonts w:ascii="..."` 会漏掉
   `w:asciiTheme="minorHAnsi"` 这类引用。
4. **默认段落样式也要合并**：很多 run 没有直接字体，继承自 `docDefaults` 和
   默认段落样式；不合并会漏掉大量文本。
5. **`w:rStyle` 的继承方向**：`basedOn` 链是“基类 -> 派生类”，合并时后出现的
   覆盖先出现的。
6. **字体名不能与系统已安装字体同名**：否则 Word 可能优先用系统字体，导致显示
   原文而不是假字形。
7. **`fsType` 必须允许嵌入**：很多商业字体 `fsType` 禁止嵌入，需要显式置 0。
8. **`OS/2` 覆盖范围置全 1**：否则 Word 认为假字体缺字，回退到系统字体。
9. **标点不混淆**：否则 `compressPunctuation`、行首行尾禁则和两端对齐会出现
   可见差异。
10. **空白不混淆**：空格、制表符、换行必须保持原码位，否则断行和缩进会变。
11. **公式文本不处理**：`m:t` 与 `w:t` 是两套文本模型，混淆会破坏 OMML。
12. **分页可能变化**：即使行高一致，字宽和 kerning 细节仍可能让个别段落重排；
    对分页敏感的场景需要 Word 导出 PDF 后人工比对。
13. **输出包顺序**：尽量沿用原 `namelist()` 顺序写入，减少 Word 修复提示。
14. **校验用映射文件**：`buckets.tsv` 是验证和调试的关键；没有它无法从输出
    反推原文，也无法生成显示层对照。

## 9. 可扩展方向

* 把 `bucket_divisor` 做成按页/按段落自适应，减少小文档的字体数量；
* 支持更多字体族（需要同时提供行高兼容的骨架）；
* 用 `fontTools` 的 `subset` 只保留实际使用的 GSUB/GPOS 特性，进一步减小体积；
* 提供 `--decrypt` 模式：读取 `buckets.tsv` 与 `odttf`，把文档还原为普通文本；
* 对 PDF 导出做像素级 diff，作为布局回归测试。

## 10. 参考

* ECMA-376 / ISO 29500：WordprocessingML 与 Embedded Font 部分；
* `fontTools` 文档：`subset`、`TTFont`、`cmap`、`glyf`、`pens`；
* 历史实验脚本：`build.py`、`build2.py`、`fakefont.py`、`bucketfont.py`、
  `normalizer.py`、`verify*.py`。
