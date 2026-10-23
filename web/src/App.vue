<script setup>
import { ref, reactive, computed, onMounted, nextTick, onUnmounted } from 'vue'
import * as echarts from 'echarts'
import { editorialTheme } from './chartTheme.js'

// ===== 固定 DeepSeek 配置（与后端 app.py 顶部保持一致）=====
// accessToken 仅当后端设置了 READER_API_TOKEN 环境变量时才需要填同样的值。
const API_CONFIG = {
  baseUrl: 'https://api.deepseek.com/v1',
  model: 'deepseek-v4-pro',
  accessToken: '',
}

const doc = ref(null)
const report = ref(null)
const tab = ref('points')
const busy = ref(false)
const err = ref('')
const notice = ref('')
const previewPage = ref(0)
const previewSrc = ref('')
const previewScale = ref(1)
const flipped = reactive({})
const chartRefs = reactive({})
let charts = []
let eventSource = null

const job = reactive({ running: false, progress: 0, message: '', elapsed: 0 })
let elapsedTimer = null
function startElapsed() {
  stopElapsed()
  job.elapsed = 0
  const t0 = Date.now()
  elapsedTimer = setInterval(() => { job.elapsed = Math.floor((Date.now() - t0) / 1000) }, 500)
}
function stopElapsed() { if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null } }
function formatElapsed(s) {
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m ${s % 60}s`
}
const estimate = reactive({ show: false, n_pages: 0, n_chunks: 0, estimated_tokens: 0,
  skipped: [], max_total_tokens: 0, max_pages: 0 })

// 文库：已分析过的文献，用于对比 / 综述（F12）
const library = reactive([])
function addToLibrary(d) {
  if (d && d.doc_id && !library.find(x => x.doc_id === d.doc_id))
    library.push({ doc_id: d.doc_id, title: d.title || d.doc_id })
}
const docTitle = id => (library.find(x => x.doc_id === id) || {}).title || id

// 问答对话历史（D13）
const askInput = ref('')
const askHistory = reactive([])
const askBusy = ref(false)

// 对比 / 综述（F12）
const compareSel = reactive(new Set())
const compareResult = ref(null)
const reviewResult = ref(null)
const compareBusy = ref(false)
const compareErr = ref('')

const TYPE_LABEL = { conclusion: '结论', method: '方法', data: '数据',
  limitation: '局限', innovation: '创新', background: '背景' }

// P1-1：所有 API 请求统一带鉴权头
function apiHeaders(contentType = false) {
  const h = {}
  if (contentType) h['Content-Type'] = 'application/json'
  if (API_CONFIG.accessToken) h['X-Access-Token'] = API_CONFIG.accessToken
  return h
}

const steps = ['上传文件', '解析', '分析', '完成']
const currentStep = computed(() => {
  if (report.value) return 3
  if (job.running) return 2
  if (doc.value) return 1
  return 0
})

function formatUploadError(body) {
  if (!body) return '上传失败'
  if (body.error === 'scanned_pdf') return '检测到扫描版 PDF，未提取到文字层。请使用带文字层的 PDF，或先经 OCR 处理后再上传。'
  if (body.error === 'unauthorized') return '访问令牌错误或未填写，请在右上角设置中填写 X-Access-Token。'
  if (body.error === 'word_convert_failed') return body.message || 'Word 转 PDF 失败，请另存为 PDF 后再上传'
  if (body.error === 'too_many_pages') return body.message || 'PDF 页数超限'
  if (body.error === 'too_many_tokens') return body.message || '预估 token 超限'
  return body.detail || body.message || '上传失败'
}

async function uploadFile(file) {
  if (!file) return
  err.value = ''
  notice.value = ''
  busy.value = true
  report.value = null
  compareResult.value = null
  reviewResult.value = null
  const fd = new FormData()
  fd.append('file', file)
  try {
    const r = await fetch('/api/upload', { method: 'POST', headers: apiHeaders(), body: fd })
    const j = await r.json()
    if (!r.ok) { err.value = formatUploadError(j); busy.value = false; return }
    doc.value = j
    if (j.source_kind === 'word')
      notice.value = 'Word 文档已自动转为 PDF 进行解析（页码与图表以转换后的 PDF 为准）。'
    await fetchEstimate()
  } catch (e) { err.value = String(e.message || e) }
  busy.value = false
}

async function fetchEstimate() {
  if (!doc.value) return
  try {
    const r = await fetch(`/api/docs/${doc.value.doc_id}/estimate`, { headers: apiHeaders() })
    const j = await r.json()
    if (r.ok) Object.assign(estimate, j, { show: true })
  } catch (e) { console.error(e) }
}

function onDrop(e) { e.preventDefault(); uploadFile(e.dataTransfer.files[0]) }
function onPick(e) { uploadFile(e.target.files[0]); e.target.value = '' }

async function startAnalyze() {
  if (estimate.show && estimate.estimated_tokens > estimate.max_total_tokens) {
    err.value = `预估 token ${estimate.estimated_tokens} 超过限制 ${estimate.max_total_tokens}`
    return
  }
  job.running = true; job.progress = 0; job.message = '排队中…'
  startElapsed()
  try {
    const r = await fetch('/api/analyze', {
      method: 'POST', headers: apiHeaders(true),
      body: JSON.stringify({ doc_id: doc.value.doc_id, base_url: API_CONFIG.baseUrl, model: API_CONFIG.model }),
    })
    const j = await r.json()
    if (!r.ok) throw new Error(formatUploadError(j) || '分析启动失败')
    connectSSE(j.job_id)
  } catch (e) { err.value = String(e.message || e); job.running = false; stopElapsed() }
}

function connectSSE(jobId) {
  closeSSE()
  const token = API_CONFIG.accessToken ? `?token=${encodeURIComponent(API_CONFIG.accessToken)}` : ''
  const es = new EventSource(`/api/jobs/${jobId}/events${token}`)
  eventSource = es
  es.onmessage = (ev) => {
    let data
    try { data = JSON.parse(ev.data) } catch { return }
    if (data.type === 'progress' || data.type === 'section_done' || data.type === 'failed_section') {
      job.progress = data.progress || job.progress
      job.message = data.message || job.message
    }
    if (data.type === 'section_done' && data.points?.length) {
      if (!report.value) report.value = { title: doc.value.title, partial: true, all_points: [] }
      for (const p of data.points) { p.section = data.section; report.value.all_points.push(p) }
    }
    if (data.type === 'done') {
      report.value = data.report
      addToLibrary({ doc_id: doc.value.doc_id, title: data.report.title })
      job.running = false; stopElapsed(); tab.value = 'points'; nextTick(renderCharts); closeSSE()
    }
    if (data.type === 'error') { err.value = data.error || '分析失败'; job.running = false; stopElapsed(); closeSSE() }
  }
  es.onerror = () => { closeSSE(); poll(jobId) }
}

function closeSSE() { if (eventSource) { eventSource.close(); eventSource = null } }

async function poll(jobId) {
  if (eventSource) return
  while (job.running) {
    await new Promise(r => setTimeout(r, 1200))
    try {
      const r = await fetch(`/api/jobs/${jobId}`, { headers: apiHeaders() })
      const j = await r.json()
      job.progress = j.progress; job.message = j.message
      if (j.status === 'done') {
        report.value = j.result
        addToLibrary({ doc_id: doc.value.doc_id, title: j.result.title })
        job.running = false; stopElapsed(); tab.value = 'points'; nextTick(renderCharts); return
      }
      if (j.status === 'error') { err.value = j.error; job.running = false; stopElapsed(); return }
    } catch (e) { console.error(e) }
  }
}

function chartRenderable(spec) {
  const labels = spec.labels || []
  const values = spec.values || []
  return labels.length >= 2 && values.length === labels.length &&
         values.every(v => typeof v === 'number' && !isNaN(v))
}

function renderCharts() {
  charts.forEach(c => c.dispose()); charts = []
  if (tab.value !== 'charts') return
  report.value?.charts?.forEach((spec, idx) => {
    if (!chartRenderable(spec)) return
    const key = spec.chart_id || `${spec.title}_${idx}`
    const el = chartRefs[key]
    if (!el) return
    const c = echarts.init(el, 'editorial')   // D8：使用抽离的编辑出版物主题
    const isLine = spec.chart_type === 'line'
    const isPie = spec.chart_type === 'pie'
    c.setOption({
      title: { text: spec.title, subtext: spec.note || '' },
      tooltip: { trigger: isPie ? 'item' : 'axis' },
      grid: { left: 52, right: 24, bottom: 44, top: 56, containLabel: true },
      xAxis: isPie ? undefined : { type: 'category', data: spec.labels },
      yAxis: isPie ? undefined : { type: 'value', name: spec.unit || '' },
      series: [{
        type: isLine ? 'line' : isPie ? 'pie' : 'bar',
        data: isPie ? spec.labels.map((l, i) => ({ name: l, value: spec.values[i] })) : spec.values,
        smooth: true,
      }],
    })
    charts.push(c)
  })
}

function onResize() { charts.forEach(c => c && c.resize()) }
function setTab(t) { tab.value = t; if (t === 'charts') nextTick(renderCharts) }

// F10：文本类导出（md / csv / json）保持 fetch + JSON
async function downloadText(path, fallback, withBom = false) {
  try {
    const r = await fetch(path, { headers: apiHeaders() })
    const j = await r.json()
    const content = withBom ? '﻿' + j.content : j.content
    const blob = new Blob([content], { type: 'text/plain;charset=utf-8' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob); a.download = j.filename || fallback; a.click()
    URL.revokeObjectURL(a.href)
  } catch (e) { err.value = String(e.message || e) }
}

// F10：二进制类（APKG / 原文 PDF）必须带鉴权头，裸 <a href> 会 401
async function downloadBlob(path, filename) {
  try {
    const r = await fetch(path, { headers: apiHeaders() })
    if (!r.ok) { err.value = '下载失败（需 X-Access-Token 令牌）'; return }
    const blob = await r.blob()
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob); a.download = filename; a.click()
    URL.revokeObjectURL(a.href)
  } catch (e) { err.value = String(e.message || e) }
}

// F10 + D7：原文页面图片 fetch + blob（裸 <img src> 因 require_token 必 401）；modal 支持翻页/缩放/键盘
async function openPreview(page) {
  if (!doc.value) return
  previewPage.value = page
  previewScale.value = 1
  try {
    const r = await fetch(`/api/pages/${doc.value.doc_id}/${page}`, { headers: apiHeaders() })
    if (!r.ok) { err.value = '原文页面加载失败（需 X-Access-Token 令牌）'; previewPage.value = 0; return }
    const blob = await r.blob()
    if (previewSrc.value) URL.revokeObjectURL(previewSrc.value)
    previewSrc.value = URL.createObjectURL(blob)
  } catch (e) { err.value = String(e.message || e); previewPage.value = 0 }
}
function closePreview() {
  previewPage.value = 0
  if (previewSrc.value) { URL.revokeObjectURL(previewSrc.value); previewSrc.value = '' }
}
function prevPage() { if (previewPage.value > 1) openPreview(previewPage.value - 1) }
function nextPage() {
  const max = (report.value && report.value.n_pages) || (doc.value && doc.value.n_pages) || 999
  if (previewPage.value < max) openPreview(previewPage.value + 1)
}
function zoomIn() { previewScale.value = Math.min(3, +(previewScale.value + 0.2).toFixed(2)) }
function zoomOut() { previewScale.value = Math.max(0.4, +(previewScale.value - 0.2).toFixed(2)) }
function onKey(e) {
  if (!previewPage.value) return
  if (e.key === 'Escape') closePreview()
  else if (e.key === 'ArrowLeft') prevPage()
  else if (e.key === 'ArrowRight') nextPage()
  else if (e.key === '+' || e.key === '=') zoomIn()
  else if (e.key === '-' || e.key === '_') zoomOut()
}

// D13：对话式问答
async function askDoc() {
  const q = askInput.value.trim()
  if (!q || !doc.value || askBusy.value) return
  askBusy.value = true
  askHistory.push({ role: 'user', text: q })
  askInput.value = ''
  try {
    const r = await fetch(`/api/docs/${doc.value.doc_id}/ask`, {
      method: 'POST', headers: apiHeaders(true),
      body: JSON.stringify({ question: q, model: API_CONFIG.model, base_url: API_CONFIG.baseUrl }),
    })
    const j = await r.json()
    askHistory.push({ role: 'ai', text: j.answer || '（无回答）', citations: j.citations || [] })
  } catch (e) { askHistory.push({ role: 'ai', text: '问答请求失败：' + (e.message || e) }) }
  askBusy.value = false
}

// F12：对比 / 综述（前端 UI 打通死功能）
function toggleDoc(id) {
  if (compareSel.has(id)) compareSel.delete(id); else compareSel.add(id)
}
async function compareDocs() {
  const ids = [...compareSel]
  if (ids.length < 2) { compareErr.value = '请至少选择 2 篇文献进行对比'; return }
  compareErr.value = ''; compareBusy.value = true; reviewResult.value = null
  try {
    const r = await fetch('/api/docs/compare', {
      method: 'POST', headers: apiHeaders(true),
      body: JSON.stringify({ doc_ids: ids, model: API_CONFIG.model, base_url: API_CONFIG.baseUrl }),
    })
    const j = await r.json()
    if (!r.ok) throw new Error(j.detail || j.message || '对比失败')
    compareResult.value = j
  } catch (e) { compareErr.value = String(e.message || e) }
  compareBusy.value = false
}
async function reviewDocs() {
  const ids = [...compareSel]
  if (ids.length < 2) { compareErr.value = '请至少选择 2 篇文献进行综述'; return }
  compareErr.value = ''; compareBusy.value = true; compareResult.value = null
  try {
    const r = await fetch('/api/docs/review', {
      method: 'POST', headers: apiHeaders(true),
      body: JSON.stringify({ doc_ids: ids, model: API_CONFIG.model, base_url: API_CONFIG.baseUrl }),
    })
    const j = await r.json()
    if (!r.ok) throw new Error(j.detail || j.message || '综述失败')
    reviewResult.value = j
  } catch (e) { compareErr.value = String(e.message || e) }
  compareBusy.value = false
}

function confidenceBadge(conf) {
  const c = parseFloat(conf) || 0
  if (c >= 0.8) return '高'
  if (c >= 0.6) return '中'
  return '低'
}
function ratingText(rating) {
  const r = parseInt(rating) || 0
  if (r >= 5) return '很高'
  if (r >= 4) return '较高'
  if (r >= 3) return '一般'
  if (r >= 2) return '较低'
  return '低'
}

onMounted(() => {
  echarts.registerTheme('editorial', editorialTheme)
  window.addEventListener('resize', onResize)
  window.addEventListener('keydown', onKey)
})
onUnmounted(() => {
  window.removeEventListener('resize', onResize)
  window.removeEventListener('keydown', onKey)
  closeSSE()
  stopElapsed()
  charts.forEach(c => c && c.dispose())
  if (previewSrc.value) URL.revokeObjectURL(previewSrc.value)
})
</script>

<template>
  <h1>文献速读 Agent</h1>
  <p class="sub">上传 PDF / Word → 结构化要点 · 数据图表 · 知识卡片 · 一键导出 · 多文献对比与综述</p>

  <div class="row" style="margin-bottom:12px">
    <template v-if="report">
      <button class="ghost" @click="downloadText(`/api/export/${doc.doc_id}/report.md`, 'report.md')">Markdown</button>
      <button class="ghost" @click="downloadText(`/api/export/${doc.doc_id}/cards.csv`, 'cards.csv', true)">CSV 卡片</button>
      <button class="ghost" @click="downloadText(`/api/export/${doc.doc_id}/report.json`, 'report.json')">JSON</button>
      <button class="ghost" @click="downloadBlob(`/api/export/${doc.doc_id}/cards.apkg`, `${report.title}-记忆卡片.apkg`)">APKG</button>
      <button class="ghost" @click="downloadBlob(`/api/source/${doc.doc_id}`, `${doc.doc_id}.pdf`)">原文 PDF</button>
    </template>
  </div>

  <div class="drop" :class="{ over: busy }" @dragover.prevent @drop="onDrop" @click="$refs.file.click()">
    <template v-if="!busy">
      <div class="big">↑</div>
      <p>拖拽 PDF / Word 到这里，或点击选择文件</p>
    </template>
    <template v-else><p>解析中…</p></template>
  </div>
  <input ref="file" type="file" accept=".pdf,.doc,.docx" style="display:none" @change="onPick" />
  <p class="tip">💡 小提示：Word 文档可直接上传，会自动转为 PDF 解析；若文档内有较多图片、图表或复杂排版，建议先在 Word 中「另存为 PDF」再上传，页码溯源与图表提取会更准确。</p>
  <p v-if="notice" class="msg" style="color:var(--brand);margin-bottom:10px">{{ notice }}</p>
  <p v-if="err" style="color:var(--red);font-size:13px;margin-bottom:10px">{{ err }}</p>

  <div v-if="estimate.show && doc && !job.running && !report" class="panel">
    <p class="msg">预估：{{ estimate.n_pages }} 页 / {{ estimate.n_chunks }} 个片段 / 约 {{ estimate.estimated_tokens }} tokens</p>
    <p v-if="estimate.skipped_sections?.length" class="msg">已跳过章节：{{ estimate.skipped_sections.join('、') }}</p>
    <button @click="startAnalyze" style="margin-top:8px">确认开始分析</button>
  </div>

  <div v-if="job.running" class="panel">
    <div class="steps">
      <template v-for="(s, i) in steps" :key="s">
        <div class="step" :class="{ on: currentStep === i, done: currentStep > i }">
          <span class="dot">{{ currentStep > i ? '✓' : i + 1 }}</span><span>{{ s }}</span>
        </div>
        <span v-if="i < steps.length - 1" class="step-line"></span>
      </template>
    </div>
    <div class="progress big"><i :style="{ width: job.progress + '%' }"></i></div>
    <div class="job-status">
      <span class="msg">{{ job.message }}</span>
      <span class="job-meta">
        <span class="job-pct">{{ job.progress }}%</span>
        <span class="job-time">已用 {{ formatElapsed(job.elapsed) }}</span>
      </span>
    </div>
  </div>

  <template v-if="report">
    <div v-if="report.failed_sections?.length" class="panel banner fail">
      <b>以下章节提取失败：</b>
      <span v-for="s in report.failed_sections" :key="s.section" class="msg" style="margin-right:10px">{{ s.section }}（{{ s.page_from }}-{{ s.page_to }} 页）</span>
    </div>

    <div v-if="report.partial || report.degraded" class="panel banner warn">
      <span v-if="report.partial">全文综合阶段失败，以下为按章节提取的原始要点。</span>
      <span v-if="report.degraded"> 当前接口不支持 JSON 模式，已降级解析。</span>
    </div>

    <div class="panel">
      <h2>{{ report.title }}</h2>
      <p class="msg" v-if="report.author">{{ report.author }} · {{ report.n_pages }} 页</p>
      <p class="msg">价值评分：<b class="rating">{{ ratingText(report.rating) }}</b>（{{ report.rating }}/5）<span class="msg" style="font-size:12px">　{{ report.rating_reason }}</span></p>
      <h3 class="sec">核心问题</h3><p>{{ report.core_question }}</p>
      <h3 class="sec">一段话总结</h3><p>{{ report.summary }}</p>
    </div>

    <div class="tabs">
      <button :class="{ on: tab === 'points' }" @click="setTab('points')">要点（{{ report.main_points.length }}）</button>
      <button :class="{ on: tab === 'charts' }" @click="setTab('charts')">图表（{{ report.charts.length }}）</button>
      <button :class="{ on: tab === 'cards' }" @click="setTab('cards')">知识卡片（{{ report.cards.length }}）</button>
      <button :class="{ on: tab === 'terms' }" @click="setTab('terms')">术语表（{{ report.terms.length }}）</button>
      <button :class="{ on: tab === 'ask' }" @click="setTab('ask')">问答</button>
      <button :class="{ on: tab === 'compare' }" @click="setTab('compare')">对比 / 综述</button>
    </div>

    <div v-if="tab === 'points'">
      <h3 class="sec">主要要点</h3>
      <div v-if="!report.main_points.length" class="empty">本文未提取到结构化要点。</div>
      <div v-for="(p, i) in report.main_points" :key="'m' + i" class="pt">
        <span class="badge" :class="'b-' + p.type">{{ TYPE_LABEL[p.type] || p.type }}</span>
        <span v-if="p.critic_status" class="badge" :class="'c-' + p.critic_status">{{ p.critic_status === 'verified' ? '已核验' : p.critic_status === 'degraded' ? '已校正' : '未通过' }}</span>
        <div class="body">
          <div>{{ p.point }}</div>
          <div class="evi" v-if="p.evidence">原文：{{ p.evidence }}</div>
          <div class="evi" v-if="p.critic_reason">critic：{{ p.critic_reason }}</div>
        </div>
        <button class="ghost small pg" @click="openPreview(p.page)">P{{ p.page }}</button>
      </div>
      <h3 class="sec">局限与风险</h3>
      <div v-if="!report.limitations.length" class="empty">无记录。</div>
      <div v-for="(x, i) in report.limitations" :key="'l' + i" class="pt">
        <span class="badge b-limitation">局限</span><div class="body">{{ x }}</div>
      </div>
      <details>
        <summary>展开全部 {{ report.all_points.length }} 条要点（按章节）</summary>
        <div v-for="(p, i) in report.all_points" :key="'a' + i" class="pt" style="margin-top:8px">
          <span class="badge" :class="'b-' + p.type">{{ TYPE_LABEL[p.type] || p.type }}</span>
          <div class="body">
            <div>{{ p.point }}</div>
            <div class="evi">{{ p.section }} · 模型自评：{{ confidenceBadge(p.confidence) }}</div>
          </div>
          <button class="ghost small pg" @click="openPreview(p.page)">P{{ p.page }}</button>
        </div>
      </details>
    </div>

    <div v-if="tab === 'charts'">
      <div v-if="!report.charts.length" class="empty">未从文献中提取到量化数据图表。</div>
      <div class="charts">
        <div v-for="(c, idx) in report.charts" :key="c.chart_id || `${c.title}_${idx}`" class="chart-box"
             :ref="el => chartRefs[c.chart_id || `${c.title}_${idx}`] = el"></div>
      </div>
      <div v-if="report.chart_unverified?.length">
        <h3 class="sec">未核验数据（未渲染进图表）</h3>
        <div v-for="u in report.chart_unverified" :key="u.title" class="pt">
          <b>{{ u.title }}</b>（{{ u.reason }}）
          <div v-for="it in u.items" :key="it.label" class="evi">{{ it.label }}: {{ it.value }}</div>
        </div>
      </div>
    </div>

    <div v-if="tab === 'cards'">
      <p class="msg" style="margin-bottom:10px">点击卡片展开答案（A 面）。</p>
      <div v-if="!report.cards.length" class="empty">本文未生成知识卡片。</div>
      <div class="cards">
        <div v-for="(c, i) in report.cards" :key="i" class="card" @click="flipped[i] = !flipped[i]">
          <div class="q">问：{{ c.front }}</div>
          <div v-if="flipped[i]" class="a">答：{{ c.back }}</div>
          <div v-else class="hint">点击查看答案</div>
          <div class="tag">{{ c.section || c.tag }}</div>
        </div>
      </div>
    </div>

    <div v-if="tab === 'terms'">
      <div v-if="!report.terms.length" class="empty">本文未提取术语表。</div>
      <div class="term-grid">
        <div v-for="(t, i) in report.terms" :key="i" class="term">
          <b>{{ t.term }}</b><p style="margin-top:4px;font-size:13px">{{ t.definition }}</p>
        </div>
      </div>
    </div>

    <div v-if="tab === 'ask'" class="panel">
      <div class="row">
        <input v-model="askInput" placeholder="输入问题，基于已提取文本回答" style="flex:1" @keyup.enter="askDoc" />
        <button @click="askDoc" :disabled="askBusy">提问</button>
      </div>
      <div class="chat">
        <div v-for="(m, i) in askHistory" :key="i" class="bubble" :class="m.role">
          <div>{{ m.text }}</div>
          <div v-if="m.citations?.length" class="cites">
            <span v-for="(c, ci) in m.citations" :key="ci">「{{ c.claim }}」
              <button class="ghost small" @click="openPreview(c.page)">P{{ c.page }}</button></span>
          </div>
        </div>
        <div v-if="!askHistory.length" class="empty">还没有对话，试着问：「本文的研究方法是什么？」</div>
      </div>
    </div>

    <div v-if="tab === 'compare'" class="panel">
      <h3 class="sec">多文献对比 / 综述</h3>
      <p class="msg">已分析的文献（点击选择 2 篇及以上）：</p>
      <div v-if="!library.length" class="empty">文库为空。请先完成至少两篇文献的分析，再回到此页对比。</div>
      <div v-else class="library">
        <span v-for="d in library" :key="d.doc_id" class="doc-chip" :class="{ sel: compareSel.has(d.doc_id) }" @click="toggleDoc(d.doc_id)">{{ d.title }}</span>
      </div>
      <div class="row">
        <button @click="compareDocs" :disabled="compareBusy || compareSel.size < 2">并排对比</button>
        <button class="ghost" @click="reviewDocs" :disabled="compareBusy || compareSel.size < 2">生成综述</button>
        <span class="msg">已选 {{ compareSel.size }} 篇</span>
      </div>
      <p v-if="compareErr" class="msg" style="color:var(--red)">{{ compareErr }}</p>

      <div v-if="compareResult && compareResult.error" class="banner fail">{{ compareResult.message }}</div>
      <div v-if="compareResult && compareResult.matrix">
        <h3 class="sec">对比矩阵</h3>
        <table class="cmp-table">
          <thead><tr><th>维度</th><th v-for="id in compareResult.doc_ids" :key="id">{{ docTitle(id) }}</th></tr></thead>
          <tbody>
            <tr v-for="(row, ri) in compareResult.matrix" :key="ri">
              <td>{{ row.dimension }}</td>
              <td v-for="id in compareResult.doc_ids" :key="id">{{ row[id] }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="compareResult.synthesis"><b>综合：</b>{{ compareResult.synthesis }}</p>
        <p v-if="compareResult.common?.length"><b>共同点：</b>{{ compareResult.common.join('；') }}</p>
        <p v-if="compareResult.gaps?.length"><b>研究空白：</b>{{ compareResult.gaps.join('；') }}</p>
      </div>

      <div v-if="reviewResult && reviewResult.error" class="banner fail">{{ reviewResult.message }}</div>
      <div v-if="reviewResult && reviewResult.review">
        <h3 class="sec">主题综述</h3>
        <p>{{ reviewResult.review }}</p>
        <h3 class="sec">论点溯源</h3>
        <div v-for="(c, ci) in reviewResult.claims" :key="ci" class="pt">
          <div class="body">{{ c.claim }}
            <span class="evi" v-for="(s, si) in c.sources" :key="si">{{ docTitle(s.doc_id) }} 第 {{ s.page }} 页　</span>
          </div>
        </div>
      </div>
    </div>
  </template>

  <div v-if="previewPage" class="modal" @click.self="closePreview">
    <button class="close" @click="closePreview" aria-label="关闭">×</button>
    <div class="tools">
      <button class="icon-btn" @click="prevPage">上一页</button>
      <span class="pageno">第 {{ previewPage }} 页</span>
      <button class="icon-btn" @click="nextPage">下一页</button>
      <button class="icon-btn" @click="zoomOut">－</button>
      <span class="pageno">{{ Math.round(previewScale * 100) }}%</span>
      <button class="icon-btn" @click="zoomIn">＋</button>
    </div>
    <div class="stage">
      <img v-if="previewSrc" :src="previewSrc" :style="{ transform: `scale(${previewScale})` }" />
    </div>
  </div>
</template>
