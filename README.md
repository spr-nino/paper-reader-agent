# 文献速读 Agent

上传 PDF / Word，自动提取结构化要点、数据图表、术语表与知识卡片，支持原文页码溯源、问答、多篇对比与 Anki 导出。

> **关于 Word 文档**：`.docx` / `.doc` 可直接上传，服务端会自动调用本机 Microsoft Word 转为 PDF 后解析（需要 Windows 且已安装 Word；其他环境请先在 Word 中「另存为 PDF」再上传）。若文档内含较多图片、图表或复杂排版，建议优先另存为 PDF 上传，页码溯源与图表提取会更准确。

---

## 🚀 一键启动整个 Agent

本项目是**前后端一体的 Agent 应用**。日常使用时，**不需要分别启动前端或后端**，直接运行下面的快捷启动脚本即可同时拉起后端服务与前端页面。

### Windows

#### 方式一：双击启动（推荐）

在文件资源管理器中双击项目根目录下的：

```
start-agent.bat
```

脚本会自动完成环境检查、依赖安装，并同时启动后端（`http://127.0.0.1:8321`）与前端开发服务器（`http://localhost:5173`），随后打开浏览器。

#### 方式二：PowerShell 启动

```powershell
# 在项目根目录打开 PowerShell，执行：
.\start-agent.ps1
```

> 如果提示“执行策略禁止运行脚本”，可以先用管理员 PowerShell 执行一次：
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```
> 然后再运行 `.\start-agent.ps1`。

### macOS / Linux

```bash
# 在项目根目录打开终端，执行：
./start-agent.sh
```

如果脚本没有执行权限，先赋予权限：

```bash
chmod +x start-agent.sh
./start-agent.sh
```

### 启动后访问

脚本启动完成后，浏览器会自动打开：

```
http://localhost:5173
```

如果浏览器没有自动打开，可以手动访问上述地址。

### 关闭 Agent

- **Windows**：关闭弹出的两个黑色命令行窗口（后端窗口 + 前端窗口）。启动器窗口按任意键仅退出启动器本身，不会停止服务。
- **macOS / Linux**：在启动脚本所在的终端按 `Ctrl + C`，脚本会自动结束后端与前端的进程。

### 如果双击 `start-agent.bat` 没反应（Windows 排错）

- **先确认装没装环境**：需要 Python 3.10+ 与 Node.js 20+，且安装时勾选了 “Add to PATH”。可分别运行 `python --version`、`node --version` 验证。
- **只弹了一下窗口就消失**：多为环境未装或脚本早期报错。新版启动器会在每一步失败时停下并显示原因；若窗口仍一闪而过，可右键 `start-agent.bat` → “以管理员身份运行”，或先打开一个命令提示符再执行 `start-agent.bat`。
- **想看详细原因**：启动器会把关键步骤写入项目根目录的 `start-agent.log`；后端/前端的实时报错则在各自弹出的黑色窗口中。
- **想单独启动某一端**（也便于定位是哪一侧的问题）：

  ```bat
  backend\run-backend.bat
  web\run-frontend.bat
  ```

- **确认文件没有被“记事本”关联**：若双击只是用记事本打开了脚本内容，说明 `.bat` 默认打开方式被改过，右键 → “打开方式” → 选择“命令提示符”，或直接用上面两条手动命令启动。

---

## ⚙️ 手动启动（仅作备用）

如果你不想使用一键脚本，或者脚本在你的环境遇到问题，可以分别启动前后端。

### 环境要求

- Python 3.10+
- Node.js 20+（推荐，与 `.nvmrc` 一致）
- （可选）Git

### 1. 启动后端

```bash
cd backend
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt

# 开发模式（仅监听 127.0.0.1，无鉴权）
uvicorn app:app --host 127.0.0.1 --port 8321 --reload
```

后端默认地址：`http://127.0.0.1:8321`

### 2. 启动前端

再开一个终端：

```bash
cd web
npm install
npm run dev
```

前端默认地址：`http://localhost:5173`

---

## 🔧 环境变量

| 变量 | 说明 | 默认值 / 建议 |
|---|---|---|
| `READER_API_TOKEN` | 访问令牌（生产环境必填） | 空（开发模式放行） |
| `ALLOWED_BASE_URLS` | OpenAI 兼容接口白名单，逗号分隔 | 空（启用内网拒绝兜底） |
| `OPENAI_API_KEY` | OpenAI 兼容 API Key | 空 |
| `OPENAI_BASE_URL` | OpenAI 兼容接口 Base URL | `https://api.openai.com/v1` |
| `READER_DATA_DIR` | 数据目录 | `./data` |
| `MAX_PAGES` | 单 PDF 最大页数 | `100` |
| `MAX_TOTAL_TOKENS` | 单次分析最大输入 token 上限 | `200000` |
| `MAX_TOTAL_GB` | `data/` 总容量上限（GB） | `2.0` |
| `MAX_AGE_DAYS` | 单文档最大保留天数 | `7.0` |
| `CORS_ORIGINS` | 允许的前端来源，逗号分隔 | `http://localhost:5173` 等 |

你可以在启动脚本所在终端通过以下方式设置（以 Windows PowerShell 为例）：

```powershell
$env:OPENAI_API_KEY="sk-..."
.\start-agent.ps1
```

---

## 📁 项目架构

```
文献文件速读助手/
├── start-agent.bat      # Windows 一键启动脚本
├── start-agent.ps1      # Windows PowerShell 一键启动脚本
├── start-agent.sh       # macOS / Linux 一键启动脚本
├── backend/             # FastAPI 后端
│   ├── app.py           # 主服务入口
│   ├── parser.py        # PDF 解析（真实页码 / 章节 / chunks / 扫描检测）
│   ├── analyzer.py      # Map-Reduce 分析管线
│   ├── critic.py        # 校验环（evidence / 数值一致性 / 幻觉语义）
│   ├── qa.py            # Q&A / 对比 / 主题综述
│   ├── charts.py        # 图表抽取与校验
│   ├── export.py        # Markdown / CSV / JSON 导出
│   ├── anki_export.py   # Anki .apkg 导出
│   └── tests/           # 测试用例
├── web/                 # Vue 3 + Vite 前端
│   ├── src/
│   │   ├── App.vue
│   │   ├── main.js
│   │   └── style.css
│   └── package.json
└── data/                # 运行期生成的解析结果与缓存
```

数据流：

```
PDF → parser.py 解析（真实页码 / 章节 / chunks / 扫描检测）
  ↓
analyzer.py Map-Reduce（要点 / 术语 / 图表 / 卡片候选）
  ↓
critic.py 校验环（evidence 存在性 / 数值一致性 / 幻觉语义）
  ↓
report.json + chunks.json + meta.json（落盘）
  ↓
前端 SSE 实时渲染 / Q&A / 对比 / 导出
```

---

## ✨ 主要功能

- **结构化速读**：Map-Reduce 管线按章节提取核心要点、方法、数据、局限与创新点。
- **真实页码**：基于 PyMuPDF 逐页解析，每个 chunk 携带真实页码区间；要点证据可一键跳转到原文 PNG。
- **图表双通道**：
  - 表格通道：PyMuPDF `find_tables()` 确定性抽取，零幻觉。
  - 视觉通道：对含图页面调用视觉模型识别，数值再与原文回验。
  - 未核验数据仅在附录列出，不会渲染进图表。
- **失败可见**：章节提取失败会在报告顶部展示失败横幅，并记录 `data/<doc_id>/errors.log`；降级模式标记 `degraded: true`。
- **扫描版拦截**：未提取到文字层的扫描版 PDF 返回明确错误，可选 Tesseract OCR 兜底。
- **状态持久化**：解析结果、任务状态、报告均落盘，后端重启可恢复；支持 LRU 自动清理。
- **SSE 实时推送**：分析过程中边分析边渲染已完成章节，无需等待假进度条。
- **Q&A / 多篇对比 / 主题综述**：基于已提取文本做检索与综合。
- **Anki 导出**：生成原生 `.apkg` 文件，直接导入 Anki 25.x。

---

## 🧪 测试

```bash
cd backend
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # macOS / Linux
python -m pytest -q
```

---

## 📝 已知限制

- 扫描版 PDF 若无 Tesseract，会明确报错而不是自动成功。
- 图表视觉通道依赖视觉模型，未核验数值不会渲染进图表。
- 公式、复杂版式表格的识别准确率受 PDF 文字层质量影响。

---

## 📄 许可证

MIT
