<template>
  <section v-if="!currentUser" class="auth-page">
    <div class="auth-card">
      <h1>医路通 AI</h1>
      <p>登录后你的报告、问诊记录和向量记忆会按账号隔离保存。</p>
      <el-input v-model="authForm.phone" size="large" placeholder="手机号（11位）" />
      <el-input v-if="authMode === 'register'" v-model="authForm.idNumber" size="large" placeholder="身份证号" />
      <el-input v-if="authMode === 'register'" v-model="authForm.displayName" size="large" placeholder="姓名（支持中文）" />
      <el-input v-model="authForm.password" size="large" placeholder="密码" show-password />
      <el-button type="primary" size="large" :loading="authLoading" @click="submitAuth">
        {{ authMode === 'login' ? '登录' : '注册并登录' }}
      </el-button>
      <button class="auth-switch" @click="authMode = authMode === 'login' ? 'register' : 'login'">
        {{ authMode === 'login' ? '没有账号？去注册' : '已有账号？去登录' }}
      </button>
    </div>
  </section>

  <div v-else class="layout">
    <aside class="sidebar">
      <div class="brand">医路通 AI</div>
      <div class="brand-sub">互联网医院智能分诊、问诊、用药、报告与预约服务平台</div>
      <div class="user-badge">
        <strong>{{ currentUser.display_name || currentUser.username }}</strong>
        <small>{{ currentUser.phone || currentUser.username }}</small>
        <button @click="logout">退出</button>
      </div>
      <button
        v-for="item in navItems"
        :key="item.key"
        class="nav-item"
        :class="{ active: activePage === item.key }"
        @click="activePage = item.key"
      >
        <span>{{ item.icon }}</span>
        <strong>{{ item.label }}</strong>
      </button>
    </aside>

    <main class="main">
      <section class="hero">
        <div>
          <h1>{{ pageMeta.title }}</h1>
          <p>{{ pageMeta.subtitle }}</p>
        </div>
        <div class="hero-tags">
          <el-tag type="success">DeepResearch 默认开启</el-tag>
          <el-tag type="primary">Swarm 多 Agent</el-tag>
          <el-tag type="warning">RAG 证据引用</el-tag>
        </div>
      </section>

      <!-- 三个 AI 场景统一对话布局 -->
      <section v-show="activePage === 'triage'" class="chat-grid">
        <ChatScene
          ref="triageSceneRef"
          scene="triage"
          :patient="patient"
          @result="r => triageResult = r"
        />
        <InsightPanel title="分诊证据与 Agent 链路" :result="triageResult" />
      </section>

      <section v-show="activePage === 'guided_triage'" class="chat-grid">
        <ChatScene
          ref="guidedTriageSceneRef"
          scene="guided_triage"
          :patient="patient"
          @result="r => guidedTriageResult = r"
        />
        <InsightPanel title="分步导诊 LangGraph 链路" :result="guidedTriageResult" />
      </section>

      <section v-show="activePage === 'consultation'" class="chat-grid">
        <ChatScene
          ref="consultSceneRef"
          scene="consultation"
          :patient="patient"
          @result="r => consultResult = r"
        />
        <InsightPanel title="问诊证据与 Agent 链路" :result="consultResult" />
      </section>

      <section v-show="activePage === 'medication'" class="chat-grid">
        <ChatScene
          ref="medicationSceneRef"
          scene="medication"
          :patient="patient"
          @result="r => medicationResult = r"
        />
        <InsightPanel title="用药证据与 Agent 链路" :result="medicationResult" />
      </section>

      <section v-show="activePage === 'medication_safety'" class="chat-grid">
        <ChatScene
          ref="medicationSafetySceneRef"
          scene="medication_safety"
          :patient="patient"
          @result="r => medicationSafetyResult = r"
        />
        <InsightPanel title="用药安全 LangChain Agent 链路" :result="medicationSafetyResult" />
      </section>

      <section v-if="activePage === 'records'" class="panel">
        <div class="panel-title">
          <h2>问诊记录</h2>
          <el-radio-group v-model="recordDays" @change="loadRecords">
            <el-radio-button :label="7">近 7 天</el-radio-button>
            <el-radio-button :label="30">近 30 天</el-radio-button>
            <el-radio-button :label="180">近半年</el-radio-button>
          </el-radio-group>
        </div>
        <div class="record-cards">
          <article v-for="record in records" :key="record.id" class="record-card" @click="openRecord(record)">
            <div class="record-top">
              <el-tag>{{ sceneLabel(record.scene) }}</el-tag>
              <span>{{ formatTime(record.created_at) }}</span>
            </div>
            <div class="record-chief">{{ record.chief_complaint || '未填写主诉' }}</div>
            <div class="record-meta">
              <span>风险：{{ record.risk_level || '-' }}</span>
              <span>科室：{{ record.department || record.recommended_department || '-' }}</span>
            </div>
            <p>{{ shortText(record.summary) }}</p>
          </article>
        </div>
      </section>

      <section v-if="activePage === 'reports'" class="panel reports-page">
        <div class="panel-title reports-toolbar">
          <h2>报告查询</h2>
          <div class="panel-actions">
            <el-button type="primary" :disabled="!selectedReport" :loading="reportLoading" @click="loadReportAnalysis">解析报告</el-button>
            <el-button v-if="selectedReport?.parse_status === 'failed'" plain :loading="reportLoading" @click="loadReportAnalysis">重新生成</el-button>
          </div>
        </div>
          <div class="report-list">
            <article
              v-for="report in reports"
              :key="report.id"
              class="report-card"
              :class="{ active: selectedReport?.id === report.id }"
              @click="openRawReport(report)"
            >
              <strong>{{ report.title || report.name }}</strong>
              <span>{{ report.type }} · {{ report.report_date || report.date }} · {{ report.status }}</span>
            </article>
          </div>
      </section>

      <section v-if="activePage === 'appointment'" class="grid">
        <div class="panel">
          <div class="panel-title">
            <h2>预约挂号</h2>
            <el-button type="primary" plain class="my-appointment-btn" @click="appointmentModalOpen = true">
              🗓 我的预约 ({{ activeAppointments.length }})
            </el-button>
          </div>
          <div class="department-grid">
            <button
              v-for="dept in departments"
              :key="dept"
              :class="{ active: selectedDepartment === dept }"
              @click="chooseDepartment(dept)"
            >
              <span>{{ dept }}</span>
            </button>
          </div>
        </div>
        <div class="panel">
          <div class="panel-title">
            <h2>{{ selectedDepartment || '请选择科室' }} 排班号源</h2>
            <div class="panel-actions">
              <el-tag type="success">未来 7 天</el-tag>
              <el-button size="small" @click="loadAppointments">刷新</el-button>
            </div>
          </div>
          <div class="schedule-cards">
            <article v-for="row in schedule" :key="row.schedule_id" class="schedule-card">
              <div class="schedule-date">
                <strong>{{ row.visit_date || row.date }}</strong>
                <span>{{ row.weekday }} · {{ row.period }}</span>
              </div>
              <div class="doctor-line">
                <span class="doctor-avatar">{{ row.doctor?.slice(0, 1) }}</span>
                <div>
                  <strong>{{ row.doctor }}</strong>
                  <small>{{ row.doctor_title || row.title }} · {{ row.time_slot }}</small>
                </div>
              </div>
              <div class="slot-meta">
                <span>剩余 {{ row.remaining ?? row.quota }} / {{ row.quota }}</span>
                <span>挂号费 {{ row.fee }} 元</span>
              </div>
              <el-button
                type="primary"
                :disabled="isBooked(row) || (row.remaining ?? row.quota) <= 0"
                @click="book(row)"
              >
                {{ isBooked(row) ? '已预约' : ((row.remaining ?? row.quota) <= 0 ? '已约满' : '预约此号') }}
              </el-button>
            </article>
          </div>
        </div>
      </section>

      <section v-if="activePage === 'settings'" class="grid">
        <div class="panel profile-panel">
          <h2>个人中心</h2>
          <p class="muted">可修改手机号、密码、基础信息和健康史。手机号修改后下次登录请使用新手机号。</p>
          <el-form class="profile-form" label-position="top">
            <el-form-item label="手机号">
              <el-input v-model="profileForm.phone" placeholder="手机号（11位）" />
            </el-form-item>
            <el-form-item label="新密码">
              <el-input v-model="profileForm.password" placeholder="不修改请留空" show-password />
            </el-form-item>
            <el-form-item label="性别">
              <el-select v-model="profileForm.gender" placeholder="性别" clearable>
                <el-option label="男" value="男" />
                <el-option label="女" value="女" />
              </el-select>
            </el-form-item>
            <el-form-item label="年龄">
              <el-input-number v-model="profileForm.age" :min="0" :max="130" placeholder="年龄" />
            </el-form-item>
            <el-form-item label="住址" class="profile-wide">
              <el-input v-model="profileForm.address" placeholder="住址" />
            </el-form-item>
            <el-form-item label="慢病史">
              <el-input v-model="profileForm.chronic_diseases" type="textarea" :rows="3" placeholder="如高血压、糖尿病等，没有可填无" />
            </el-form-item>
            <el-form-item label="过敏史">
              <el-input v-model="profileForm.allergy_history" type="textarea" :rows="3" placeholder="如青霉素、海鲜过敏等，没有可填无" />
            </el-form-item>
            <el-form-item label="用药史">
              <el-input v-model="profileForm.medication_history" type="textarea" :rows="3" placeholder="长期或近期正在使用的药物，没有可填无" />
            </el-form-item>
          </el-form>
          <div class="profile-actions">
            <el-button type="primary" :loading="profileSaving" @click="saveProfile">保存个人资料</el-button>
          </div>
        </div>
        <div class="panel">
          <h2>系统设置</h2>
          <div class="mini-grid">
            <div><span>远程模型</span><strong>{{ settings.llm_enabled ? '已启用' : '未启用' }}</strong></div>
            <div><span>API 地址</span><strong>{{ settings.base_url || '未配置' }}</strong></div>
            <div><span>联网检索</span><strong>默认开启</strong></div>
          </div>
          <div class="answer-card">
            API Key 和 Base URL 从 backend/config/config.yaml 读取。当前页面不会展示密钥明文，避免泄露。
          </div>
        </div>
      </section>

      <!-- 问诊记录详情 -->
      <el-dialog v-model="recordDialogVisible" title="问诊记录详情" width="760px">
        <div v-if="selectedRecord" class="record-detail">
          <h3>主诉</h3>
          <p>{{ selectedRecord.chief_complaint || '-' }}</p>
          <h3>推荐科室</h3>
          <el-tag size="large" type="success">{{ selectedRecord.department || selectedRecord.recommended_department || '-' }}</el-tag>
          <h3>风险等级</h3>
          <el-tag size="large" type="warning">{{ selectedRecord.risk_level || '-' }}</el-tag>
          <h3>完整摘要</h3>
          <div class="markdown-body" v-html="renderMd(selectedRecord.summary || '')"></div>
        </div>
      </el-dialog>

      <el-dialog v-model="reportDialogVisible" title="AI 报告解析" width="860px" class="report-analysis-dialog">
        <div v-if="selectedReport" class="report-dialog-head">
          <strong>{{ selectedReport.title || selectedReport.file_name }}</strong>
          <el-button type="primary" plain @click="openRawReport(selectedReport)">打开源文件</el-button>
        </div>
        <div v-if="reportAnalysis" class="answer-card markdown-body compact-analysis" v-html="renderMd(reportAnalysis)"></div>
        <div v-else class="empty-state">暂无解析内容。</div>
      </el-dialog>

      <el-dialog v-model="reportPreviewVisible" :title="previewReport?.title || previewReport?.file_name || '源文件预览'" width="900px" class="report-preview-dialog">
        <iframe
          v-if="previewReport?.doc_id && isPdfReport(previewReport)"
          class="raw-preview-frame"
          :src="rawDocUrl(previewReport.doc_id)"
        ></iframe>
        <el-image
          v-else-if="previewReport?.doc_id && isImageReport(previewReport)"
          class="raw-preview-image"
          :src="rawDocUrl(previewReport.doc_id)"
          fit="contain"
        />
        <div v-else class="empty-state">该文件类型无法内嵌预览，可点击下方打开源文件。</div>
        <template #footer>
          <el-button v-if="previewReport?.doc_id" type="primary" @click="window.open(rawDocUrl(previewReport.doc_id), '_blank')">新窗口打开</el-button>
        </template>
      </el-dialog>

      <!-- 我的预约弹窗 -->
      <el-dialog v-model="appointmentModalOpen" title="我的预约" width="640px">
        <div v-if="appointments.length" class="appointment-list">
          <article
            v-for="item in appointments"
            :key="item.id"
            class="appointment-card"
            :class="{ cancelled: item.status === '已取消' }"
          >
            <div class="appointment-info">
              <strong>{{ item.department }} · {{ item.doctor }}</strong>
              <span>{{ item.doctor_title }} · {{ item.visit_date }} {{ item.period }} {{ item.time_slot }}</span>
              <small>预约单号：YL{{ String(item.id).padStart(6, '0') }} · {{ item.status }}</small>
            </div>
            <el-button
              v-if="item.status === '已预约'"
              size="small"
              type="danger"
              plain
              @click="cancelBooked(item)"
            >
              取消
            </el-button>
          </article>
        </div>
        <div v-else class="friendly-empty">暂无预约记录</div>
      </el-dialog>
    </main>
  </div>
</template>

<script setup>
import { computed, defineComponent, h, nextTick, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { marked } from 'marked'
import {
  cancelAppointment,
  createAppointment,
  getAppointments,
  deleteSession,
  getDepartments,
  getMetrics,
  getMedicalDocument,
  getMedicalDocuments,
  getRecords,
  getSessionMessages,
  getSchedule,
  getSettings,
  getMe,
  getStoredUser,
  getSessions,
  interpretReport,
  login,
  rawDocUrl,
  register,
  streamMedicalChat,
  updateMe,
  uploadMedicalDocumentsConcurrent,
  clearAuth
} from './api/client'
// 说明：旧的 runTriage / runConsultation / runMedication / sendChat 已下线，统一改走 streamMedicalChat。

marked.setOptions({ gfm: true, breaks: true })

const currentUser = ref(getStoredUser())
const authMode = ref('login')
const authLoading = ref(false)
const authForm = ref({ phone: '', password: '', displayName: '', idNumber: '' })
const profileSaving = ref(false)
const profileForm = ref({
  phone: '',
  password: '',
  gender: '',
  age: null,
  address: '',
  chronic_diseases: '',
  allergy_history: '',
  medication_history: ''
})

const navItems = [
  { key: 'triage', label: '智能分诊台', icon: '◆' },
  { key: 'guided_triage', label: '分步导诊台', icon: '◎' },
  { key: 'consultation', label: '线上问诊', icon: '●' },
  { key: 'medication_safety', label: '用药安全助手', icon: '✚' },
  { key: 'records', label: '问诊记录', icon: '○' },
  { key: 'reports', label: '报告查询', icon: '▲' },
  { key: 'appointment', label: '预约挂号', icon: '◇' },
  { key: 'settings', label: '系统设置', icon: '⚙' }
]

const pageMap = {
  triage: ['智能分诊台', '输入症状与患者基础信息，一次输出风险分层、推荐科室和可追溯分诊依据。'],
  guided_triage: ['分步导诊台', 'LangGraph 结构化追问：先收集症状、持续时间、严重程度等信息，信息充分后再给出分诊结论。'],
  consultation: ['线上问诊', '面向连续对话的健康科普问诊，支持上传报告/影像、短期记忆和联网增强。'],
  medication_safety: ['用药安全助手', 'LangChain Tool Agent：检索知识库、读取用药史/过敏史、规则扫描与联网补充，评估用药相互作用与安全风险。'],
  records: ['问诊记录', '查询近 7 天、30 天和半年内的分诊、问诊、用药记录。'],
  reports: ['报告查询', '展示检验检查报告，并使用 AI 解读异常原因、复查建议和推荐科室。'],
  appointment: ['预约挂号', '按科室查看未来 7 天上午/下午医生排班与剩余号源。'],
  settings: ['系统设置', '查看远程模型、联网检索和数据管理配置。']
}

const activePage = ref('triage')
const pageMeta = computed(() => {
  const [title, subtitle] = pageMap[activePage.value] || pageMap.triage
  return { title, subtitle }
})

const patient = ref({
  age: 26,
  gender: '男',
  chronic_diseases: '',
  allergy_history: '',
  medication_history: ''
})

const triageResult = ref(null)
const guidedTriageResult = ref(null)
const consultResult = ref(null)
const medicationResult = ref(null)
const medicationSafetyResult = ref(null)

const metrics = ref({})
const settings = ref({})
const records = ref([])
const reports = ref([])
const departments = ref([])
const schedule = ref([])
const appointments = ref([])
const selectedDepartment = ref('')
const selectedReport = ref(null)
const reportAnalysis = ref('')
const recordDays = ref(7)
const selectedRecord = ref(null)
const recordDialogVisible = ref(false)
const appointmentModalOpen = ref(false)
const reportLoading = ref(false)
const reportDialogVisible = ref(false)
const reportPreviewVisible = ref(false)
const previewReport = ref(null)
const activeAppointments = computed(() => appointments.value.filter(item => item.status === '已预约'))

function renderMd(text) {
  if (!text) return ''
  try {
    return marked.parse(String(text))
  } catch {
    return String(text)
  }
}

// =============== 通用 ChatScene 组件 ===============
const ChatScene = defineComponent({
  name: 'ChatScene',
  props: {
    scene: { type: String, required: true },
    patient: { type: Object, required: true }
  },
  emits: ['result'],
  setup(props, { emit, expose }) {
    const greetings = {
      triage: '您好，我是分诊助手。请描述主要症状、持续时间、是否伴随发热/疼痛等。我会综合分析、给出风险等级和建议挂号的科室。',
      guided_triage: '您好，我是分步导诊助手。我会先通过几轮结构化追问，了解您的主要症状、持续时间和严重程度，信息收集充分后再给出风险分层和推荐科室。',
      consultation: '您好，我是您的线上问诊助手。可以连续追问，也可以上传化验单、CT/B超报告，我会结合您的报告和症状给出健康科普建议。',
      medication: '您好，我是用药咨询助手。请告诉我您想了解的药品 / 症状 / 合并用药情况，也可以上传处方或药品说明书图片。',
      medication_safety: '您好，我是用药安全助手。请告诉我您正在使用的药物、想合用的新药，以及过敏史/慢病情况。我会调用知识库和工具分析相互作用与用药风险。'
    }
    const messages = ref([{ role: 'assistant', content: greetings[props.scene] || '请描述您的情况。' }])
    const sessionId = ref(null)
    const sessionList = ref([])
    const attachments = ref([])
    const inputText = ref('')
    const loading = ref(false)
    const fileInputRef = ref(null)
    const scrollRef = ref(null)
    const storageKey = `medical_agent_session_${props.scene}`
    const allowAttachments = !['triage', 'guided_triage', 'medication_safety'].includes(props.scene)

    function isImageFile(file) { return /^image\//.test(file.type) }
    async function scrollEnd() {
      await nextTick()
      if (scrollRef.value) scrollRef.value.scrollTop = scrollRef.value.scrollHeight
    }
    function removeAttachment(idx) {
      const item = attachments.value[idx]
      if (item?.previewUrl?.startsWith('blob:')) URL.revokeObjectURL(item.previewUrl)
      attachments.value.splice(idx, 1)
    }
    function isImageDoc(att) {
      const name = (att.file_name || '').toLowerCase()
      return /\.(png|jpe?g|webp|bmp|gif)$/.test(name)
    }
    function isPdfDoc(att) {
      return (att.file_name || '').toLowerCase().endsWith('.pdf')
    }
    function fileBadge(att) {
      const name = (att.file_name || '').toLowerCase()
      if (name.endsWith('.pdf')) return 'PDF'
      if (/\.(docx?|rtf)$/.test(name)) return 'DOC'
      if (/\.(xlsx?|csv)$/.test(name)) return 'XLS'
      return 'FILE'
    }
    function badgeClass(att) {
      const b = fileBadge(att)
      return {
        PDF: 'file-thumb pdf',
        DOC: 'file-thumb doc',
        XLS: 'file-thumb xls',
        FILE: 'file-thumb generic'
      }[b]
    }
    function statusLabel(item) {
      if (item.status === 'uploading') return item.stage || '上传中'
      if (item.status === 'error') return '失败'
      if (item.parse_status === 'failed') return '已保存待复核'
      if (item.status === 'done') return item.duration_ms ? `已解析 ${formatDuration(item.duration_ms)}` : (item.chroma_chunks ? `已入库 ${item.chroma_chunks} 段` : '已解析')
      return ''
    }
    function restoreMessage(row) {
      return {
        role: row.role,
        content: row.content || '',
        attachments: row.attachments || [],
        risk_level: row.metadata?.risk_level || '',
        recommended_department: row.metadata?.department || '',
        phase: row.metadata?.phase || ''
      }
    }
    async function restoreSession() {
      const saved = localStorage.getItem(storageKey)
      await loadSceneSessions()
      if (!saved) return
      try {
        const rows = await getSessionMessages(saved)
        if (!rows.length) return
        sessionId.value = saved
        messages.value = rows.map(restoreMessage)
        await scrollEnd()
      } catch {
        localStorage.removeItem(storageKey)
      }
    }
    async function loadSceneSessions() {
      try {
        sessionList.value = await getSessions(props.scene)
      } catch {
        sessionList.value = []
      }
    }
    async function openSession(id) {
      try {
        const rows = await getSessionMessages(id)
        sessionId.value = id
        localStorage.setItem(storageKey, id)
        messages.value = rows.length ? rows.map(restoreMessage) : [{ role: 'assistant', content: greetings[props.scene] }]
        attachments.value = []
        emit('result', null)
        await scrollEnd()
      } catch {
        ElMessage.error('会话加载失败')
      }
    }
    async function removeSession(id) {
      try {
        await ElMessageBox.confirm('确认删除这个历史对话吗？', '删除对话', {
          type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消'
        })
        await deleteSession(id)
        if (sessionId.value === id) newChat()
        await loadSceneSessions()
        ElMessage.success('对话已删除')
      } catch (err) {
        if (err !== 'cancel') ElMessage.error('删除失败')
      }
    }

    async function onPickFiles(evt) {
      if (!allowAttachments) {
        ElMessage.info('分诊场景不使用附件报告，请直接描述症状')
        return
      }
      const files = Array.from(evt.target.files || [])
      evt.target.value = ''
      if (!files.length) return
      const remain = 9 - attachments.value.length
      if (remain <= 0) { ElMessage.warning('最多 9 个附件'); return }
      if (files.length > remain) ElMessage.warning(`只接受前 ${remain} 个附件，其余被忽略`)
      const accepted = files.slice(0, remain).filter(f => f.size <= 20 * 1024 * 1024)
      if (!accepted.length) return
      const startIdx = attachments.value.length
      const placeholders = accepted.map((file, i) => ({
        localId: `${Date.now()}-${startIdx + i}`,
        file_name: file.name,
        isImage: isImageFile(file),
        previewUrl: isImageFile(file) ? URL.createObjectURL(file) : '',
        status: 'uploading',
        stage: '上传原始文件',
        doc_id: null
      }))
      attachments.value.push(...placeholders)
      const results = await uploadMedicalDocumentsConcurrent(accepted, {
        sessionId: sessionId.value,
        onItemDone: (idx, res) => {
          const t = attachments.value[startIdx + idx]
          if (!t) return
          if (res.ok) {
            t.status = 'done'
            t.stage = '解析完成'
            t.doc_id = res.data.doc_id
            t.title = res.data.title
            t.doc_type = res.data.doc_type
            t.confidence = res.data.confidence
            t.parse_status = res.data.status
            t.summary = res.data.summary
            t.chroma_chunks = res.data.chroma_chunks || 0
            t.duration_ms = res.data.duration_ms || 0
            t.pipeline_steps = res.data.pipeline_steps || []
            if (res.data.status === 'failed') {
              t.stage = '远程解析失败，已保存待复核'
            }
          } else {
            t.status = 'error'
            t.error = res.error
            t.stage = '解析失败'
          }
        },
        onProgress: evt => {
          const percent = evt.total ? Math.round((evt.loaded / evt.total) * 100) : 0
          placeholders.forEach(item => {
            if (item.status === 'uploading') item.stage = percent >= 100 ? '解析文档并向量化' : `上传中 ${percent}%`
          })
        }
      }, 5)
      const failed = results.filter(r => r && !r.ok)
      if (failed.length) ElMessage.error(`${failed.length} 个文件失败：${failed.map(f => f.fileName).join('、')}`)
      const degraded = results.filter(r => r?.ok && r.data?.status === 'failed')
      if (degraded.length) ElMessage.warning(`${degraded.length} 个文件已保存，但远程解析暂时失败，可稍后重试解析`)
      if (results.some(r => r?.ok)) refreshAfterAction()
    }

    async function submit() {
      if (!inputText.value.trim() && !attachments.value.length) {
        ElMessage.warning('请输入问题或附加文件')
        return
      }
      if (attachments.value.some(a => a.status === 'uploading')) {
        ElMessage.warning('还有附件正在解析，请稍候')
        return
      }
      const text = inputText.value
      const readyAtts = allowAttachments ? attachments.value.filter(a => a.status === 'done' && a.doc_id) : []
      const attachedDocIds = allowAttachments ? readyAtts.map(a => a.doc_id) : []
      const attMeta = readyAtts.map(a => ({
        doc_id: a.doc_id, file_name: a.file_name, title: a.title,
        doc_type: a.doc_type, summary: a.summary, confidence: a.confidence, duration_ms: a.duration_ms
      }))

      messages.value.push({ role: 'user', content: text, attachments: attMeta })
      inputText.value = ''
      attachments.value = []
      const aIdx = messages.value.push({ role: 'assistant', content: '', streaming: true }) - 1
      loading.value = true
      await scrollEnd()
      let buffer = ''
      const traceList = []
      const result = {
        answer: '', risk_level: '', recommended_department: '',
        evidence: [], agent_trace: [], thinking_steps: [], _streaming: true
      }
      emit('result', { ...result, _key: Date.now() })

      try {
        await streamMedicalChat(props.scene, {
          message: text,
          session_id: sessionId.value || undefined,
          patient_context: { ...props.patient },
          attached_doc_ids: attachedDocIds,
          enable_deep_search: true
        }, {
          onSession: e => {
            if (e.session_id) {
              sessionId.value = e.session_id
              localStorage.setItem(storageKey, e.session_id)
              loadSceneSessions()
            }
          },
          onTrace: e => {
            traceList.push({ agent: e.agent, action: e.action, detail: e.detail })
            messages.value[aIdx].thinking_status = e.detail
            result.agent_trace = [...traceList]
            result.thinking_steps = traceList.map(t => `${t.agent}: ${t.detail}`)
            emit('result', { ...result, _key: Date.now() })
          },
          onEvidence: e => {
            result.evidence = e.items || []
            emit('result', { ...result, _key: Date.now() })
          },
          onChunk: async delta => {
            buffer += delta
            messages.value[aIdx].content = buffer
            result.answer = buffer
            emit('result', { ...result, _key: Date.now() })
            await scrollEnd()
          },
          onPhase: e => {
            result.phase = e.phase || 'collecting'
            result.completeness = e.completeness
            result.questions_asked = e.questions_asked
            messages.value[aIdx].phase = e.phase
            emit('result', { ...result, _key: Date.now() })
          },
          onDone: e => {
            messages.value[aIdx].content = buffer
            messages.value[aIdx].streaming = false
            messages.value[aIdx].phase = e.phase || (e.risk_level ? 'completed' : 'collecting')
            messages.value[aIdx].risk_level = e.risk_level
            messages.value[aIdx].recommended_department = e.recommended_department
            result.answer = buffer
            result.phase = e.phase || (e.risk_level ? 'completed' : 'collecting')
            result.risk_level = e.risk_level
            result.recommended_department = e.recommended_department
            result.evidence = e.evidence || []
            result.agent_trace = e.agent_trace || []
            result.thinking_steps = e.thinking_steps || []
            result.metrics = e.metrics || {}
            result.completeness = e.metrics?.completeness
            result.questions_asked = e.metrics?.questions_asked
            result._streaming = false
            emit('result', { ...result, _key: Date.now() })
            refreshAfterAction()
          }
        }).promise
      } catch (err) {
        messages.value[aIdx].content = (buffer || '') + `\n\n（请求失败：${err?.message || '未知错误'}）`
        messages.value[aIdx].streaming = false
      } finally {
        loading.value = false
        await scrollEnd()
      }
    }

    function newChat() {
      messages.value = [{ role: 'assistant', content: greetings[props.scene] }]
      attachments.value = []
      sessionId.value = null
      localStorage.removeItem(storageKey)
      emit('result', null)
    }
    function clearChat() { newChat() }

    onMounted(restoreSession)

    expose({ clearChat })

    return () => h('div', { class: 'panel chat-panel' }, [
      h('div', { class: 'chat-head' }, [
        h('h2', sceneTitle(props.scene)),
        h('div', { class: 'chat-head-actions' }, [
          h('span', { class: 'stream-badge' }, '流式回答'),
          h('button', { class: 'new-chat-btn', onClick: newChat }, '新对话')
        ])
      ]),
      h('div', { class: 'chat-body' }, [
      h('aside', { class: 'session-history' }, [
        h('strong', '历史对话'),
        sessionList.value.length
          ? h('div', { class: 'session-list' }, sessionList.value.map(s =>
              h('div', { class: ['session-item-wrap', { active: s.id === sessionId.value }] }, [
                h('button', {
                  class: 'session-item',
                  onClick: () => openSession(s.id),
                  title: s.title || s.id
                }, [
                  h('span', s.title || '未命名对话'),
                  h('small', formatTime(s.updated_at || s.created_at))
                ]),
                h('details', { class: 'session-menu' }, [
                  h('summary', '···'),
                  h('button', { onClick: () => removeSession(s.id) }, '删除对话')
                ])
              ])
            ))
          : h('small', { class: 'session-empty' }, '暂无历史')
      ]),
      h('div', { class: 'chat-stream', ref: scrollRef }, messages.value.map((m, i) =>
        h('div', { class: ['bubble-row', m.role], key: i }, [
          h('div', { class: ['avatar', m.role] }, m.role === 'user' ? '我' : 'AI'),
          h('div', { class: 'bubble-col' }, [
            m.attachments && m.attachments.length
              ? h('div', { class: 'msg-attachments' }, m.attachments.map(att =>
                  h('div', { class: 'msg-attachment', key: att.doc_id }, [
                    isImageDoc(att)
                      ? h('img', {
                          src: rawDocUrl(att.doc_id),
                          class: 'thumb',
                          onClick: () => window.open(rawDocUrl(att.doc_id), '_blank')
                        })
                      : h('a', { href: rawDocUrl(att.doc_id), target: '_blank', class: ['thumb', badgeClass(att), isPdfDoc(att) ? 'pdf-preview' : ''] }, [
                          h('span', fileBadge(att)),
                          h('small', att.file_name),
                          isPdfDoc(att) ? h('em', '点击打开 PDF') : null
                        ]),
                    h('div', { class: 'att-caption' }, att.title || att.file_name)
                  ])
                ))
              : null,
            m.content
              ? h('div', {
                  class: ['bubble', m.role, { streaming: m.streaming }],
                  innerHTML: renderMd(m.content)
                })
              : (m.streaming ? h('div', { class: 'bubble assistant streaming bubble-empty' }, m.thinking_status || '正在思考...') : null),
            (m.streaming && m.thinking_status && m.content)
              ? h('div', { class: 'thinking-line' }, m.thinking_status)
              : null,
            (m.role === 'assistant' && !m.streaming && m.phase === 'collecting')
              ? h('div', { class: 'bubble-chips' }, [
                  h('span', { class: 'chip dept' }, '📋 信息收集中，请继续回答追问')
                ])
              : null,
            (m.role === 'assistant' && !m.streaming && (m.risk_level || m.recommended_department) && m.phase !== 'collecting')
              ? h('div', { class: 'bubble-chips' }, [
                  m.risk_level ? h('span', { class: ['chip', riskClass(m.risk_level)] }, `🩺 ${m.risk_level}`) : null,
                  m.recommended_department ? h('span', { class: 'chip dept' }, `🏥 建议挂号：${m.recommended_department}`) : null,
                  m.recommended_department ? h('button', {
                    class: 'chip chip-action',
                    onClick: () => goAppointmentExternal(m.recommended_department)
                  }, '去预约 →') : null
                ])
              : null
          ])
        ])
      )),
      ]),
      attachments.value.length
        ? h('div', { class: 'attachment-tray' }, attachments.value.map((item, idx) =>
            h('div', { class: 'attachment-chip', key: item.localId }, [
              item.status === 'uploading'
                ? h('div', { class: 'chip-loading' }, [h('div', { class: 'chip-spin' }), h('small', statusLabel(item))])
                : item.status === 'error'
                  ? h('div', { class: 'chip-error' }, '×')
                  : (item.isImage
                      ? h('img', { class: 'chip-thumb', src: item.previewUrl })
                      : h('div', { class: ['chip-pdf', `chip-${fileBadge({ file_name: item.file_name }).toLowerCase()}`] }, fileBadge({ file_name: item.file_name }))),
              h('div', { class: 'chip-name', title: item.file_name }, item.file_name),
              item.duration_ms ? h('div', { class: 'chip-meta' }, `解析耗时 ${formatDuration(item.duration_ms)}`) : (item.pipeline_steps?.length ? h('div', { class: 'chip-meta' }, item.pipeline_steps.at(-1)) : null),
              h('button', { class: 'chip-close', onClick: () => removeAttachment(idx) }, '×')
            ])
          ))
        : null,
      h('div', { class: 'composer-bar' }, [
        allowAttachments ? h('input', {
          ref: fileInputRef,
          type: 'file', multiple: true, accept: 'image/*,.pdf,.docx,.doc,.xlsx,.xls,.csv',
          style: 'display:none',
          onChange: onPickFiles
        }) : null,
        allowAttachments ? h('button', {
          class: 'icon-btn',
          disabled: loading.value || attachments.value.length >= 9,
          onClick: () => fileInputRef.value?.click(),
          title: '附件（最多 9 个）'
        }, [h('span', '📎'), h('small', `${attachments.value.length}/9`)]) : null,
        h('textarea', {
          class: 'composer-input',
          value: inputText.value,
          rows: 2,
          placeholder: composerPlaceholder(props.scene),
          onInput: e => inputText.value = e.target.value,
          onKeydown: e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) submit() }
        }),
        h('button', {
          class: 'send-btn',
          disabled: loading.value,
          onClick: submit
        }, loading.value ? '生成中...' : '发送')
      ])
    ])
  }
})

function sceneTitle(scene) {
  return {
    triage: '智能分诊',
    guided_triage: '分步导诊',
    consultation: '线上问诊',
    medication: '用药咨询',
    medication_safety: '用药安全助手'
  }[scene] || '对话'
}
function composerPlaceholder(scene) {
  return {
    triage: '请描述症状、持续时间、伴随表现 (Ctrl+Enter 发送)',
    guided_triage: '请回答导诊追问，或补充症状细节 (Ctrl+Enter 发送)',
    consultation: '继续描述症状或追问建议 (Ctrl+Enter 发送)',
    medication: '请输入药品/症状/合并用药情况 (Ctrl+Enter 发送)',
    medication_safety: '例如：阿司匹林和布洛芬能一起吃吗？我有胃病 (Ctrl+Enter 发送)'
  }[scene] || ''
}
function riskClass(risk) {
  if (risk === '高风险') return 'risk-high'
  if (risk === '中风险') return 'risk-mid'
  return 'risk-low'
}
function goAppointmentExternal(dept) {
  activePage.value = 'appointment'
  if (dept && departments.value.includes(dept)) chooseDepartment(dept)
}

// =============== InsightPanel ===============
const InsightPanel = defineComponent({
  props: { title: String, result: { type: Object, default: null } },
  setup(props) {
    return () => {
      const r = props.result
      const ready = r && !r._streaming
      return h('div', { class: 'panel insight-panel' }, [
        h('h2', props.title),
        r
          ? h('div', [
              ready ? h('div', { class: 'mini-grid' }, [
                h('div', [h('span', '导诊阶段'), h('strong', r.phase === 'completed' ? '已出结论' : '信息收集中')]),
                h('div', [h('span', '完整度'), h('strong', r.completeness != null ? `${Math.round(r.completeness * 100)}%` : '-')]),
                h('div', [h('span', '风险等级'), h('strong', r.risk_level || '-')]),
                h('div', [h('span', '推荐科室'), h('strong', r.recommended_department || '-')]),
                h('div', [h('span', '证据数量'), h('strong', String((r.evidence || []).length))]),
                h('div', [h('span', 'Agent 步骤'), h('strong', String((r.agent_trace || r.thinking_steps || []).length))])
              ]) : h('div', { class: 'mini-grid placeholder' }, [
                h('div', '风险等级 · 待生成'),
                h('div', '推荐科室 · 待生成'),
                h('div', '证据数量 · 流式中'),
                h('div', 'Agent 步骤 · 流式中')
              ]),
              h('h3', '思考过程'),
              h('div', { class: 'trace-list' }, (r.agent_trace || []).map(item =>
                h('div', { class: 'trace-item' }, [
                  h('strong', item.agent),
                  h('small', item.action || ''),
                  h('span', item.detail || '')
                ])
              )),
              h('h3', '证据引用'),
              h('div', { class: 'evidence' }, (r.evidence || []).map((it, i) =>
                h('div', { class: 'evidence-item' }, [
                  h('strong', `#${i + 1} ${it.title || it.source}`),
                  h('small', `${it.source || ''} · ${it.score ?? ''}`),
                  h('p', (it.content || '').slice(0, 240))
                ])
              ))
            ])
          : h('div', { class: 'empty-state' }, '提交问题后这里会显示 Agent 调用链、证据引用和分析过程。')
      ])
    }
  }
})

// =============== Records, Reports, Appointments ===============
async function refreshAfterAction() {
  await Promise.all([loadRecords(), loadMetrics(), loadAppointments(), loadReports()])
}
async function loadRecords() { records.value = await getRecords(recordDays.value) }
async function loadMetrics() { metrics.value = await getMetrics() }
async function loadReports() {
  const docs = await getMedicalDocuments()
  reports.value = docs.map(doc => ({
    ...doc,
    id: doc.doc_id,
    type: doc.doc_type || '上传报告',
    report_date: doc.created_at,
    parse_status: doc.parse_status || doc.status,
    status: doc.confidence ? `AI解析 · 置信度 ${doc.confidence}${doc.duration_ms ? ` · ${(doc.duration_ms / 1000).toFixed(1)}秒` : ''}` : 'AI解析'
  }))
  if (!selectedReport.value && reports.value.length) selectedReport.value = reports.value[0]
}
async function loadDepartments() {
  departments.value = await getDepartments()
  selectedDepartment.value = departments.value[0] || ''
  if (selectedDepartment.value) await chooseDepartment(selectedDepartment.value)
}
async function loadSettings() { settings.value = await getSettings() }
async function chooseDepartment(department) {
  selectedDepartment.value = department
  const data = await getSchedule(department)
  schedule.value = data.schedule || []
}
async function loadAppointments() {
  appointments.value = await getAppointments()
  if (selectedDepartment.value) {
    const data = await getSchedule(selectedDepartment.value)
    schedule.value = data.schedule || []
  }
}
function scheduleKey(row) { return row.schedule_id || `${selectedDepartment.value}|${row.doctor}|${row.visit_date || row.date}|${row.period}|${row.time_slot}` }
function appointmentKey(item) { return `${item.department}|${item.doctor}|${item.visit_date}|${item.period}|${item.time_slot}` }
function isBooked(row) {
  const key = scheduleKey(row)
  return activeAppointments.value.some(item => appointmentKey(item) === key)
}
async function book(row) {
  if (isBooked(row)) { ElMessage.info('该时段已预约，可选其他时段'); return }
  const payload = { department: selectedDepartment.value, ...row }
  const data = await createAppointment(payload)
  appointments.value = data.appointments || await getAppointments()
  await chooseDepartment(selectedDepartment.value)
  ElMessage.success(`预约成功：${selectedDepartment.value} ${row.visit_date || row.date} ${row.period}`)
  appointmentModalOpen.value = true
}
async function cancelBooked(item) {
  await ElMessageBox.confirm(`确认取消 ${item.department} ${item.visit_date} ${item.period} 的预约吗？`, '取消预约', {
    type: 'warning', confirmButtonText: '确认取消', cancelButtonText: '保留'
  })
  const data = await cancelAppointment(item.id)
  appointments.value = data.appointments || await getAppointments()
  await chooseDepartment(selectedDepartment.value)
  ElMessage.success('预约已取消')
}
function selectReport(report) { selectedReport.value = report; reportAnalysis.value = '' }
function isPdfReport(report) {
  return (report?.file_name || '').toLowerCase().endsWith('.pdf')
}
function isImageReport(report) {
  return /\.(png|jpe?g|webp|bmp)$/i.test(report?.file_name || '')
}
function openRawReport(report) {
  selectedReport.value = report
  previewReport.value = report
  reportPreviewVisible.value = true
}
async function loadReportAnalysis() {
  if (!selectedReport.value) return
  reportLoading.value = true
  try {
    if (selectedReport.value.doc_id) {
      const detail = await getMedicalDocument(selectedReport.value.doc_id)
      const parsed = detail.parsed_json || {}
      selectedReport.value = { ...selectedReport.value, ...detail, items: parsed.items || [], parse_status: parsed.parse_status }
      const sections = []
      sections.push(`**${detail.title || parsed.title || detail.file_name || '报告'}**`)
      if (parsed.summary || detail.summary) sections.push(`**摘要**\n${parsed.summary || detail.summary}`)
      if ((parsed.key_abnormalities || []).length) sections.push(`**关键异常**\n${parsed.key_abnormalities.map(x => `• ${x}`).join('\n')}`)
      if (parsed.findings) sections.push(`**影像所见**\n${parsed.findings}`)
      if (parsed.impression) sections.push(`**影像诊断 / 结论**\n${parsed.impression}`)
      if ((parsed.recommendations || []).length) sections.push(`**报告建议**\n${parsed.recommendations.map(x => `• ${x}`).join('\n')}`)
      if (parsed.raw_text || parsed.raw) sections.push(`**原文节选**\n${String(parsed.raw_text || parsed.raw).slice(0, 1800)}`)
      if (parsed.parse_status === 'failed') sections.push('当前报告解析不完整，可重新上传或点击重新生成入口。')
      reportAnalysis.value = sections.join('\n\n') || '暂无解析记录'
      reportDialogVisible.value = true
    } else {
      const data = await interpretReport(selectedReport.value.id)
      reportAnalysis.value = data.analysis || data.interpretation || data.answer || '暂无解读'
      reportDialogVisible.value = true
    }
  } finally { reportLoading.value = false }
}
function openRecord(record) { selectedRecord.value = record; recordDialogVisible.value = true }

function shortText(text = '') { return text.length > 72 ? `${text.slice(0, 72)}...` : text }
function sceneLabel(scene) {
  return ({
    triage: '分诊',
    guided_triage: '分步导诊',
    consultation: '问诊',
    medication: '用药',
    medication_safety: '用药安全'
  })[scene] || scene || '记录'
}
function formatTime(value) { if (!value) return '-'; return String(value).replace('T', ' ').slice(0, 16) }
function formatDuration(ms) {
  const value = Number(ms || 0)
  if (!value) return '未知'
  return value < 1000 ? `${value}ms` : `${(value / 1000).toFixed(1)}秒`
}

async function loadInitialData() {
  await Promise.all([loadMetrics(), loadRecords(), loadReports(), loadDepartments(), loadSettings(), loadAppointments()])
}

async function submitAuth() {
  if (!/^1\d{10}$/.test(authForm.value.phone || '') || !authForm.value.password) {
    ElMessage.warning('请输入 11 位手机号和密码')
    return
  }
  if (authMode.value === 'register' && (!authForm.value.displayName || !authForm.value.idNumber)) {
    ElMessage.warning('注册需要填写姓名和身份证号')
    return
  }
  if (authMode.value === 'register') {
    const idNumber = String(authForm.value.idNumber || '').trim()
    if (![15, 18].includes(idNumber.length)) {
      ElMessage.warning('身份证号必须为 15 或 18 位')
      return
    }
    if (idNumber.length === 18 && !/^\d{17}[\dXx]$/.test(idNumber)) {
      ElMessage.warning('身份证号格式不正确，18 位身份证最后一位可以是数字或 X')
      return
    }
    if (idNumber.length === 15 && !/^\d{15}$/.test(idNumber)) {
      ElMessage.warning('身份证号格式不正确')
      return
    }
  }
  authLoading.value = true
  try {
    const data = authMode.value === 'login'
      ? await login(authForm.value.phone, authForm.value.password)
      : await register(authForm.value.phone, authForm.value.password, authForm.value.displayName, authForm.value.idNumber)
    currentUser.value = data.user
    syncProfileForm(data.user)
    await loadInitialData()
    ElMessage.success('登录成功')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err.message || '登录失败')
  } finally {
    authLoading.value = false
  }
}

function syncProfileForm(user = currentUser.value) {
  profileForm.value = {
    phone: user?.phone || user?.username || '',
    password: '',
    gender: user?.gender || '',
    age: user?.age ?? null,
    address: user?.address || '',
    chronic_diseases: user?.chronic_diseases || '',
    allergy_history: user?.allergy_history || '',
    medication_history: user?.medication_history || ''
  }
  patient.value = {
    ...patient.value,
    age: user?.age ?? patient.value.age,
    gender: user?.gender || patient.value.gender
  }
}

async function saveProfile() {
  if (!/^1\d{10}$/.test(profileForm.value.phone || '')) {
    ElMessage.warning('手机号必须是 11 位数字')
    return
  }
  profileSaving.value = true
  try {
    const payload = {
      phone: profileForm.value.phone,
      gender: profileForm.value.gender,
      age: profileForm.value.age,
      address: profileForm.value.address,
      chronic_diseases: profileForm.value.chronic_diseases,
      allergy_history: profileForm.value.allergy_history,
      medication_history: profileForm.value.medication_history
    }
    if (profileForm.value.password) payload.password = profileForm.value.password
    const user = await updateMe(payload)
    currentUser.value = user
    syncProfileForm(user)
    ElMessage.success('个人资料已更新')
  } catch (err) {
    ElMessage.error(err?.response?.data?.detail || err.message || '保存失败')
  } finally {
    profileSaving.value = false
  }
}

function logout() {
  clearAuth()
  currentUser.value = null
  ;['triage', 'guided_triage', 'consultation', 'medication', 'medication_safety'].forEach(scene => localStorage.removeItem(`medical_agent_session_${scene}`))
}

onMounted(async () => {
  if (!currentUser.value) return
  try {
    currentUser.value = await getMe()
    syncProfileForm(currentUser.value)
    await loadInitialData()
  } catch {
    logout()
  }
})
</script>
