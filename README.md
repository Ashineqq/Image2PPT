<p align="center">
  <img src="assets/logo.png" alt="DeckWeaver logo" width="420">
</p>

# DeckWeaver

中文 | [English](README_EN.md)

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.x-3776AB?style=flat-square&logo=python&logoColor=white">
  <img alt="PowerPoint" src="https://img.shields.io/badge/Output-PPTX-D24726?style=flat-square&logo=microsoftpowerpoint&logoColor=white">
  <img alt="OCR" src="https://img.shields.io/badge/OCR-Cloud%20or%20Local-16a34a?style=flat-square">
  <img alt="Cloud API optional" src="https://img.shields.io/badge/Cloud%20API-optional-0ea5e9?style=flat-square">
  <img alt="Bitmap formats" src="https://img.shields.io/badge/Bitmap-PNG%20%7C%20JPG%20%7C%20WebP%20%7C%20TIFF-f59e0b?style=flat-square">
  <img alt="PDF input" src="https://img.shields.io/badge/Input-PDF-DC2626?style=flat-square&logo=adobeacrobatreader&logoColor=white">
</p>

DeckWeaver 可以把 GPT、Gemini等输出的图片重建为可编辑的 PowerPoint 文件。它可以作为Agent的Skill使用，或者直接作为一个无大模型介入的命令行工具。

几乎所有的图片上的文字都会被还原成可编辑的 PPT 文本框，图标、Logo、图片和装饰元素等也会拆成独立图片对象。

## 优点

- 几乎所有文字和图标都可以在 PowerPoint 里继续编辑、移动或替换。
- Token 消耗很低（作为命令行工具甚至可以无token消耗）：主体流程使用 OCR 和图像算法，不需要把整页反复交给云端多模态模型。
- 生成速度快：多页 OCR 合并成一个云端任务，或复用本地热模型，后续页面流水线自动完成。
- OCR 可跑在云端或本地：云端走百度 AI Studio 的 PP-OCRv5（同一个模型，不下载本地权重、不占本地 CPU），本地走 `paddleocr` 包。两条路径输出完全一致，下游无感。详见下文「OCR 后端」。
- 支持常见位图输入：PNG、JPG/JPEG、WebP、BMP、TIF/TIFF。
- 支持 PDF 输入：直接读取 PDF 文字层，跳过 OCR，文字 100% 还原（详见下文）。

## 效果示例

下图左侧是输入的原图（GPT 生成的隐私计算示意图），右侧是 DeckWeaver 自动重建后的 `.pptx` 经 LibreOffice 渲染回 PNG 的预览。文字全部是可编辑文本框，MPC 中心锁、连接线等都拆成了独立可移动 PNG 对象。

<table>
  <tr>
    <td align="center"><b>输入原图</b></td>
    <td align="center"><b>重建 PPT（preview）</b></td>
  </tr>
  <tr>
    <td><img src="assets/examples/source-privacy-compute.png" alt="原图" width="100%"></td>
    <td><img src="assets/examples/rendered-privacy-compute.png" alt="重建 PPT 预览" width="100%"></td>
  </tr>
</table>


## 快速开始

### 方式一：作为 Skill / Agent 工具使用


1. 克隆项目，或者直接克隆到你的 skill 目录：

```bash
git clone https://github.com/GuopengLin/Image2PPT.git
```

2. 用 Codex、Claude Code 或其他本地 agent 打开项目目录。


3. 把要转换的图片或图片文件夹告诉 agent，例如：

```text
请使用这个项目里的 skill，把 /path/to_dir 文件夹下的所有图片转换成一个可编辑 PPT。
```

也可以指定单张图片：

```text
请使用这个项目里的 skill，把 /path/to/page_01.png 转换成可编辑 PPT。
```

**注意** 首次运行时，agent 会先执行 `bash scripts/bootstrap.sh` 安装依赖，可能需要一定的耗时。最终结果在 `output/<run>/slides.pptx`。

### 方式二：作为独立命令行工具使用

适合以下几类用户：

- 不想要消耗token

- 对于文字正确性要求不高（因为不会有大模型介入文字识别）

- 想要借助命令行实现大批量识别

```bash
git clone https://github.com/GuopengLin/Image2PPT.git
cd Image2PPT
bash scripts/bootstrap.sh
```

`bootstrap.sh` 会安装 Python 依赖、本地 OCR 依赖、LibreOffice/Poppler 预览工具，并预下载模型缓存。macOS 和常见 Linux 发行版可直接使用；Windows 或受管环境可参考 `requirements.txt` 手动安装依赖。


然后一键运行：

```bash
python scripts/convert.py --source /path/to/slides
```

也可以直接处理单张图片：

```bash
python scripts/convert.py --source /path/to/page_01.png
```

或者直接传一份 PDF，每一页 PDF 会对应输出 PPT 的一页：

```bash
python scripts/convert.py --source /path/to/deck.pdf
# 调整渲染清晰度（默认 300 DPI）
python scripts/convert.py --source /path/to/deck.pdf --pdf-dpi 200
```

PDF 模式会用 PyMuPDF 直接抽取 PDF 文字层（exact Unicode + 每字 bbox，confidence=1.0），跳过 OCR 流程，文字与原文 100% 一致；同时把每页栅格化成 PNG 供后续 erase / inventory / 布局校准复用。若 PDF 是纯扫描件（无文字层），建议先把它转成图片文件夹再走图片输入路径。

生成结果会在：

```text
output/<run>/
├── slides.pptx       # 最终可编辑 PPT
├── qa.json           # PPTX 结构检查报告
├── previews/         # 预览图，用于人工比对
├── ocr/              # OCR 与可选人工复核文件
├── layouts/          # 页面布局 JSON
├── assets/           # 提取出的图片对象
└── debug/            # 调试可视化
```

如果需要调试，也可以把一键流程拆成三步手动运行：

```bash
RUN="output/demo_$(date +%Y%m%d)"
SRC="slides"

python scripts/ocr/prepare_ocr.py \
  --source-dir "$SRC" \
  --work-dir "$RUN"

python scripts/ocr/ocr_review_apply.py --work-dir "$RUN"

python scripts/build_deck.py \
  --source-dir "$SRC" \
  --work-dir "$RUN"
```

如果 `build_deck.py` 提示有 OCR 不确定项，可以打开 `ocr/page_NN.ocr_review.annotated.png` 检查高亮文字，修改对应 `ocr_review.json` 的 `corrected_text` 后重新运行后两步。
默认情况下，只要没有使用 `--skip-render`，`build_deck.py` 会先渲染文字校准预览来校准字号，再多轮渲染校准文本框位置。

### 方式三：作为 Web 前后端服务部署

适合以下几类用户：

- 想给团队/朋友提供一个浏览器界面，不希望每个人都自己装环境
- 需要账号密码鉴权、任务排队、历史记录、ETA 等基本服务化能力
- 希望服务能自动跟随 GitHub 更新（可选）

Web 层是**完全独立的额外入口**，不影响方式一、二的任何用法：不启动 web 层时 CLI 行为完全一致。所有运行时状态（SQLite、上传、产物）都写在 `web/data/` 下，已加入 `.gitignore`。

依赖也是**分开声明**的：CLI 用户只装 `requirements.txt`，永远不会装到 `fastapi` / `uvicorn` 等 web 层依赖；要部署 web 时再单独装 `web/backend/requirements.txt`。`start.sh` 会自动帮你装这一份。

```bash
git clone https://github.com/GuopengLin/Image2PPT.git
cd Image2PPT
bash scripts/bootstrap.sh                  # CLI 依赖（PaddleOCR、LibreOffice、…）
cp web/.env.example web/.env               # 至少改 ADMIN_PASSWORD 和 JWT_SECRET
bash web/start.sh                          # 自动装 web 层依赖，再起 uvicorn :8000 + vite :5173
```

打开 `http://localhost:5173`，用 `web/.env` 里设置的账号密码登录，即可上传图片 / PDF / 图片 zip，选择「完整模式」或「仅文字模式」，提交后在任务列表里看进度、ETA、下载产物，或删除历史记录。顶栏会显示当前 commit、与 `main` 的差距，并在配置 `DECKWEAVER_AUTO_UPDATE=true` 时自动拉取新版重启。

生产部署（前端编译进静态文件，单端口直接由 uvicorn 提供）：

```bash
bash web/start-prod.sh                     # 默认监听 0.0.0.0:8000
PORT=9000 bash web/start-prod.sh
```

更多 API 细节、ETA 公式、自动更新流程、用户管理 CLI（`web/backend/manage.py`）等说明见 [web/README.md](web/README.md)。

## OCR 后端

文字识别（PP-OCRv5）有两种跑法，产出完全一样，下游流程不需要区分：

| 后端 | 说明 |
| --- | --- |
| `remote`（云端） | 把页面提交给百度 AI Studio 的 PaddleOCR 服务。同一个 PP-OCRv5 模型，本地不下载 ~85 MB 权重、不占用本地 CPU。整份输入只提交**一个任务**（多页会打包成一个多页 PDF 一起上传），所以页数越多优势越明显。 |
| `local`（本地） | 用本机 `paddleocr` 包在进程内推理，需要一个可用的 `paddlepaddle` 运行时（CPU 或 GPU 版）。完全离线，不发起任何网络请求。 |

`--ocr-backend auto` 是默认值：**配了 Token 就走云端，没配就走本地**。想强制某一种就显式传 `--ocr-backend remote` / `--ocr-backend local`。

### 配置 Token

到百度 AI Studio 申请一个 Access Token（个人免费额度对本项目完全够用）：<https://aistudio.baidu.com/account/accessToken>

拿到后三选一即可，优先级从高到低：

```bash
# 1. 直接传给命令
python scripts/convert.py --source slides --ocr-token "<token>"

# 2. 环境变量
export PADDLE_OCR_TOKEN="<token>"

# 3. 写进共享配置文件（与 image-to-editable-ppt 技能共用）
editppt config --paddle-ocr-token "<token>"    # 写入 ~/.editppt/config.yaml
```

只配一次就长期生效。云端后端会把页面上传出去，**涉密或不便外发的图请改用 `--ocr-backend local`**。

### 云端后端相关参数

- `--ocr-backend auto|remote|local`：选择识别后端，默认 `auto`。
- `--ocr-token <token>`：云端 Token，默认读 `$PADDLE_OCR_TOKEN`，再读 `~/.editppt/config.yaml`。
- `--ocr-model PP-OCRv5`：云端模型名。**不要改成 `PaddleOCR-VL-*`**：那个模型返回的是版面块（一整段一个框、多行合并且无分隔符），而下游布局需要"一行一个框"，换成它会让每个文本框变粗、文字错行。
- `--remote-crop-rescue`：允许第 4 引擎的文字块放大复检也走云端（每个红标条目一次任务，较慢）。默认关闭；本地后端始终在进程内做这一步。
- `--skip-cross-verify`：跳过 EasyOCR/Tesseract 交叉验证，只跑主识别，最快。

### 表格识别后端

`--detect-tables` 打开后，候选区域交给表格结构识别，同样有两种跑法：

| 后端 | 说明 |
| --- | --- |
| `remote`（云端） | 百度 AI Studio 的 **PP-StructureV3**。它会自己检测表格、返回结构 HTML，**并顺带识别每个单元格的文字**——所以不需要拿 OCR 结果去回填单元格，合并单元格也更可靠。 |
| `local`（本地） | PaddleX 的 **SLANet_plus**（~8 MB），只做结构、不认文字，单元格文字由上游 OCR 按 bbox 归属回填。 |

和 OCR 一样，`--table-backend auto`（默认）配了 Token 就走云端，否则走本地。显式传 `--table-backend remote|local` 可强制。

一个需要注意的差异：`--table-score-threshold` 默认 0.85 原本是卡 SLANet 的**结构置信度**，而云端不返回同口径的结构分，`table_remote.py` 上报的是表格块的**版面检测置信度**（实测干净表格 0.97~0.99）。闸门还在、仍然能拦住装饰性色块，但两个后端的数字不是同一把尺子，阈值不可直接照搬。

表格识别只在 `--detect-tables` 时启用；它还会经过"规则表外观检查"和"单元格填充率 ≥ 50%"两道闸门，所以普通的卡片式横幅不会被误判成表格。

## 常用参数

```bash
python scripts/convert.py --source slides --pages 1,3,8
python scripts/convert.py --source slides --skip-render
python scripts/convert.py --source slides --detect-tables
python scripts/convert.py --source slides --icon-review
python scripts/convert.py --source slides --ocr-backend remote --ocr-token "<token>"
python scripts/convert.py --source slides --ocr-backend local
python scripts/convert.py --source slides --detect-tables --table-backend remote
python scripts/ocr/prepare_ocr.py --pages 1,3,8 ...
python scripts/convert.py --source slides --skip-cross-verify
python scripts/build_deck.py --skip-render ...
python scripts/build_deck.py --detect-tables ...
python scripts/build_deck.py --icon-review ...
```

- `--pages`：只处理指定页。
- `--ocr-backend` / `--ocr-token` / `--ocr-model` / `--remote-crop-rescue`：OCR 后端选择与 Token，见上节「OCR 后端」。
- `--detect-tables` / `--table-backend` / `--table-score-threshold`：表格识别，见上节「表格识别后端」。
- `--skip-render`：跳过 LibreOffice 预览渲染；默认的文字校准也会随之跳过。
- `--skip-calibration`：只跳过基于预览的字号与位置校准。
- `--font-calibration-iterations 1`：设置字号校准迭代次数。
- `--calibration-iterations 4`：设置文本位置校准迭代次数。
- `--detect-tables`：尝试把规则表格还原为 PPT 原生表格。
- `--icon-review` / `--icon-decisions`：导出图标/文字边界判断包，便于人工复核。
- `--pdf-dpi 300`：PDF 输入时的渲染清晰度，默认 300；页数多或对体积敏感时可降到 200。

## 项目结构

```text
.
├── assets/            # Logo 等项目资源
├── scripts/
│   ├── convert.py    # 一键转换入口
│   ├── ocr/          # OCR（本地 ocr_paddle.py / 云端 ocr_remote.py）、交叉验证、复核应用、PDF 文字层抽取
│   ├── page/         # 单页擦除文字、检测元素、生成布局
│   ├── deck/         # 合并布局并生成 PPTX
│   ├── verify/       # PPTX 检查与预览渲染
│   ├── tables/       # 表格识别（本地 table_recognize.py / 云端 table_remote.py）
│   └── optional/     # 可选后处理工具
├── references/       # 布局格式与流程说明
├── SKILL.md          # skill 工作流说明
└── requirements.txt
```

## 注意事项

- 一键入口支持单张图片、图片文件夹或 PDF 文件；图片没有按 `page_NN.<ext>` 命名时会自动复制到运行目录并按页码编号；PDF 输入则由 `scripts/ocr/pdf_ingest.py` 直接渲染并抽取文字层。
- OCR 默认在配了 Token 时走云端。云端会把页面图像上传到百度 AI Studio；不希望外发就加 `--ocr-backend local`。表格识别同理，加 `--table-backend local`。
- `bootstrap.sh` 仍然会安装本地 PaddleOCR；如果确定只用云端后端，本地那套权重可以省掉（`python scripts/warmup.py --ocr-backend remote` 会跳过下载）。
- 多页 PPT 最好保持一致比例；PowerPoint 一个文件只能有一种页面尺寸。
- 复杂图表目前会优先作为可移动图片对象保留，而不是还原为可编辑数据图表。
- 生成文件默认写入 `output/`，该目录不会提交到 Git。

## 致谢与第三方声明

本项目的部分 PPTX 布局构建、布局格式说明、重建流程说明和 PPTX 检查工具参考并改编自 [soulmujoco/EditableImage2PPTSkill](https://github.com/soulmujoco/EditableImage2PPTSkill)。原项目使用 MIT License，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 联系方式

商业授权、定制开发或问题反馈：1015277323@qq.com

## License

个人免费使用、复制和修改，但副本或修改版需要注明来源并保留项目名、版权声明、许可证和原始仓库链接。商业使用、商业分发、SaaS/内部生产系统集成等场景需要先联系作者购买商业授权。详见 [LICENSE](LICENSE)。
