const { createApp, ref, reactive, computed, watch, nextTick } = Vue

const TITLES = {
  dashboard: '首页',
  users: '用户管理',
  reports: '报告管理',
  sessions: '问诊记录',
  kb: '知识库'
}

const SCENES = {
  consultation: '线上问诊',
  triage: '智能分诊',
  medication: '用药咨询'
}

const Pager = {
  props: ['state', 'load'],
  template: `
    <div class="pager">
      <el-pagination
        background
        layout="total, sizes, prev, pager, next, jumper"
        :total="state.total"
        :page-size="state.pageSize"
        :current-page="state.page"
        :page-sizes="[10, 20, 50, 100]"
        @current-change="p => load(p)"
        @size-change="s => { state.pageSize = s; load(1) }"
      />
    </div>
  `
}

createApp({
  components: { Pager },
  setup() {
    const view = ref('dashboard')
    const tabs = ref([{ key: 'dashboard', label: '首页' }])
    const apiBase = ref(localStorage.getItem('admin_api_base') || 'http://127.0.0.1:8022')
    const session = ref(localStorage.getItem('admin_session') || '')
    const authenticated = ref(Boolean(session.value))
    const loginLoading = ref(false)
    const loginForm = reactive({ username: 'admin', password: '' })

    const loading = reactive({ dashboard: false, users: false, reports: false, sessions: false, kb: false })
    const stats = reactive({ database: {}, chroma: {} })
    const breakdown = reactive({ scenes: [], doc_types: [], sessions_daily: [], reports_daily: [], messages_daily: [] })

    const users = reactive({ rows: [], total: 0, page: 1, pageSize: 10, q: '' })
    const reports = reactive({ rows: [], total: 0, page: 1, pageSize: 10, userId: '', q: '', selected: [] })
    const sessions = reactive({ rows: [], total: 0, page: 1, pageSize: 10, userId: '', scene: '', selected: [] })
    const kb = reactive({
      files: [],
      pickedFiles: [],
      page: 1,
      pageSize: 10,
      busy: false,
      progress: 0,
      progressText: '',
      rebuilding: false,
      rebuildProgress: 0
    })

    const userDialog = reactive({
      open: false,
      tab: 'appointments',
      data: null,
      reports: [],
      sessions: [],
      appointments: [],
      loadingReports: false,
      loadingSessions: false,
      loadingAppointments: false
    })
    const reportDialog = reactive({ open: false, data: null })
    const sessionDialog = reactive({ open: false, data: null })
    const sceneChartRef = ref(null)
    const docTypeChartRef = ref(null)
    const trendChartRef = ref(null)
    let sceneChart = null
    let docTypeChart = null
    let trendChart = null

    const viewTitle = computed(() => TITLES[view.value] || '')
    const metrics = computed(() => [
      { label: '用户总数', value: stats.database?.users },
      { label: '报告总数', value: stats.database?.reports },
      { label: '会话总数', value: stats.database?.sessions },
      { label: '消息总数', value: stats.database?.messages },
      { label: '问诊记录', value: stats.database?.encounters },
      { label: '预约记录', value: stats.database?.appointments },
      { label: '知识库向量', value: stats.chroma?.kb_count },
      { label: '报告向量', value: stats.chroma?.user_reports_count }
    ])
    const kbPagedFiles = computed(() => {
      const start = (kb.page - 1) * kb.pageSize
      return kb.files.slice(start, start + kb.pageSize)
    })

    function text(value, fallback = '无') {
      if (value === null || value === undefined || value === '') return fallback
      return String(value)
    }

    function gender(value) {
      const v = text(value, '')
      if (!v) return '无'
      const lower = v.toLowerCase()
      if (['male', 'm', '男'].includes(lower)) return '男'
      if (['female', 'f', '女'].includes(lower)) return '女'
      return v
    }

    function json(value) {
      try { return JSON.stringify(value || {}, null, 2) } catch { return '{}' }
    }

    function size(n) {
      n = Number(n || 0)
      if (n < 1024) return `${n} B`
      if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
      return `${(n / 1024 / 1024).toFixed(2)} MB`
    }

    function confidence(value) {
      const n = Number(value || 0)
      if (!n) return '无'
      return n <= 1 ? `${Math.round(n * 100)}%` : String(n)
    }

    function confidenceType(value) {
      const n = Number(value || 0)
      const pct = n <= 1 ? n * 100 : n
      if (pct >= 80) return 'success'
      if (pct >= 60) return 'warning'
      return 'danger'
    }

    function sceneName(value) {
      return SCENES[value] || value || '无'
    }

    function sceneType(value) {
      if (value === 'consultation') return 'success'
      if (value === 'triage') return 'warning'
      return 'info'
    }

    async function request(path, options = {}) {
      const headers = {
        ...(options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
        'X-Admin-Session': session.value,
        ...(options.headers || {})
      }
      const resp = await fetch(apiBase.value + path, { ...options, headers })
      if (resp.status === 401) {
        clearSession()
        throw new Error('登录已失效，请重新登录')
      }
      if (!resp.ok) {
        const body = await resp.text().catch(() => '')
        throw new Error(body || `HTTP ${resp.status}`)
      }
      return resp.json()
    }

    async function login() {
      loginLoading.value = true
      try {
        clearSession()
        apiBase.value = apiBase.value.replace(/\/$/, '')
        localStorage.setItem('admin_api_base', apiBase.value)
        const data = await fetch(apiBase.value + '/api/admin/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(loginForm)
        }).then(async resp => {
          if (!resp.ok) throw new Error(await resp.text())
          return resp.json()
        })
        session.value = data.session
        authenticated.value = true
        localStorage.setItem('admin_session', session.value)
        ElementPlus.ElMessage.success('登录成功')
        await loadDashboard()
      } catch (err) {
        authenticated.value = false
        ElementPlus.ElMessage.error(`登录失败：${err.message}`)
      } finally {
        loginLoading.value = false
      }
    }

    function clearSession() {
      session.value = ''
      authenticated.value = false
      localStorage.removeItem('admin_session')
    }

    async function logout() {
      try {
        if (session.value) await request('/api/admin/logout', { method: 'POST' })
      } catch {}
      clearSession()
    }

    async function confirmDanger(message) {
      try {
        await ElementPlus.ElMessageBox.confirm(
          `${message}\n\n该操作会直接修改 PostgreSQL、Chroma 或源文件，删除后不可恢复。`,
          '危险操作确认',
          { type: 'warning', confirmButtonText: '确认执行', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' }
        )
        return true
      } catch {
        return false
      }
    }

    function openView(key) {
      view.value = key
      if (!tabs.value.find(t => t.key === key)) tabs.value.push({ key, label: TITLES[key] })
      if (!authenticated.value) return
      if (key === 'dashboard') loadDashboard()
      if (key === 'users' && !users.rows.length) loadUsers(1)
      if (key === 'reports' && !reports.rows.length) loadReports(1)
      if (key === 'sessions' && !sessions.rows.length) loadSessions(1)
      if (key === 'kb' && !kb.files.length) loadKb()
    }

    function closeTab(key) {
      const idx = tabs.value.findIndex(t => t.key === key)
      if (idx < 0) return
      tabs.value.splice(idx, 1)
      if (view.value === key) view.value = (tabs.value[idx - 1] || tabs.value[0]).key
    }

    async function loadDashboard() {
      loading.dashboard = true
      try {
        const [data, b] = await Promise.all([
          request('/api/admin/stats'),
          request('/api/admin/stats/breakdown')
        ])
        Object.assign(stats, data)
        Object.assign(breakdown, b)
        await nextTick()
        renderCharts()
      } catch (err) {
        ElementPlus.ElMessage.error(err.message)
      } finally {
        loading.dashboard = false
      }
    }

    function renderCharts() {
      if (!window.echarts || !sceneChartRef.value) return
      sceneChart = sceneChart || echarts.init(sceneChartRef.value)
      docTypeChart = docTypeChart || echarts.init(docTypeChartRef.value)
      trendChart = trendChart || echarts.init(trendChartRef.value)
      sceneChart.setOption({
        tooltip: { trigger: 'item' },
        legend: { bottom: 0 },
        series: [{
          type: 'pie',
          radius: ['42%', '68%'],
          data: (breakdown.scenes || []).map(x => ({ name: sceneName(x.scene), value: x.c }))
        }]
      })
      docTypeChart.setOption({
        tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
        grid: { left: 70, right: 20, top: 20, bottom: 30 },
        xAxis: { type: 'value' },
        yAxis: { type: 'category', data: (breakdown.doc_types || []).map(x => x.doc_type || '未分类') },
        series: [{ type: 'bar', data: (breakdown.doc_types || []).map(x => x.c), itemStyle: { color: '#1890ff' } }]
      })
      const dates = Array.from(new Set([
        ...(breakdown.sessions_daily || []).map(x => x.d),
        ...(breakdown.reports_daily || []).map(x => x.d),
        ...(breakdown.messages_daily || []).map(x => x.d)
      ])).filter(Boolean).sort()
      const byDate = rows => Object.fromEntries((rows || []).map(x => [x.d, x.c]))
      const ss = byDate(breakdown.sessions_daily)
      const rs = byDate(breakdown.reports_daily)
      const ms = byDate(breakdown.messages_daily)
      trendChart.setOption({
        tooltip: { trigger: 'axis' },
        legend: { top: 0 },
        grid: { left: 48, right: 20, top: 42, bottom: 36 },
        xAxis: { type: 'category', data: dates },
        yAxis: { type: 'value' },
        series: [
          { name: '会话', type: 'line', smooth: true, data: dates.map(d => ss[d] || 0) },
          { name: '报告', type: 'line', smooth: true, data: dates.map(d => rs[d] || 0) },
          { name: '消息', type: 'line', smooth: true, data: dates.map(d => ms[d] || 0) }
        ]
      })
    }

    window.addEventListener('resize', () => {
      sceneChart?.resize()
      docTypeChart?.resize()
      trendChart?.resize()
    })

    async function loadUsers(page = users.page) {
      loading.users = true
      try {
        users.page = page
        const params = new URLSearchParams({ q: users.q, limit: users.pageSize, offset: (page - 1) * users.pageSize })
        const data = await request(`/api/admin/users?${params}`)
        users.rows = data.users || []
        users.total = data.total || 0
      } catch (err) {
        ElementPlus.ElMessage.error(err.message)
      } finally {
        loading.users = false
      }
    }

    function resetUsers() {
      users.q = ''
      loadUsers(1)
    }

    async function openUser(userId) {
      try {
        userDialog.data = await request(`/api/admin/users/${encodeURIComponent(userId)}`)
        userDialog.tab = 'appointments'
        userDialog.reports = []
        userDialog.sessions = []
        userDialog.appointments = userDialog.data.appointments || []
        userDialog.open = true
        await loadUserTab('appointments')
      } catch (err) {
        ElementPlus.ElMessage.error(err.message)
      }
    }

    async function loadUserTab(tab) {
      const userId = userDialog.data?.user?.user_id
      if (!userId) return
      if (tab === 'appointments') {
        userDialog.loadingAppointments = true
        try {
          const data = await request(`/api/admin/users/${encodeURIComponent(userId)}/appointments?limit=100`)
          userDialog.appointments = data.appointments || []
        } catch (err) {
          ElementPlus.ElMessage.error(err.message)
        } finally {
          userDialog.loadingAppointments = false
        }
      }
      if (tab === 'reports') {
        userDialog.loadingReports = true
        try {
          const data = await request(`/api/admin/reports?user_id=${encodeURIComponent(userId)}&limit=100`)
          userDialog.reports = data.reports || []
        } catch (err) {
          ElementPlus.ElMessage.error(err.message)
        } finally {
          userDialog.loadingReports = false
        }
      }
      if (tab === 'sessions') {
        userDialog.loadingSessions = true
        try {
          const data = await request(`/api/admin/sessions?user_id=${encodeURIComponent(userId)}&limit=100`)
          userDialog.sessions = data.sessions || []
        } catch (err) {
          ElementPlus.ElMessage.error(err.message)
        } finally {
          userDialog.loadingSessions = false
        }
      }
    }

    watch(() => userDialog.tab, loadUserTab)

    async function deleteUser(userId) {
      if (!(await confirmDanger(`确认删除用户 ${userId} 及其全部关联数据吗？`))) return
      try {
        await request(`/api/admin/users/${encodeURIComponent(userId)}?cascade=true`, { method: 'DELETE' })
        ElementPlus.ElMessage.success('用户已删除')
        await Promise.all([loadUsers(users.page), loadDashboard()])
      } catch (err) {
        ElementPlus.ElMessage.error(err.message)
      }
    }

    async function loadReports(page = reports.page) {
      loading.reports = true
      try {
        reports.page = page
        const params = new URLSearchParams({
          user_id: reports.userId,
          q: reports.q,
          limit: reports.pageSize,
          offset: (page - 1) * reports.pageSize
        })
        const data = await request(`/api/admin/reports?${params}`)
        reports.rows = data.reports || []
        reports.total = data.total || 0
        reports.selected = []
      } catch (err) {
        ElementPlus.ElMessage.error(err.message)
      } finally {
        loading.reports = false
      }
    }

    function resetReports() {
      reports.userId = ''
      reports.q = ''
      loadReports(1)
    }

    function onReportSelect(rows) {
      reports.selected = rows.map(row => row.doc_id)
    }

    async function openReport(docId) {
      try {
        const data = await request(`/api/admin/reports/${encodeURIComponent(docId)}`)
        reportDialog.data = data.report
        reportDialog.open = true
      } catch (err) {
        ElementPlus.ElMessage.error(err.message)
      }
    }

    async function deleteReport(docId) {
      if (!(await confirmDanger(`确认删除报告 ${docId} 吗？`))) return
      try {
        await request(`/api/admin/reports/${encodeURIComponent(docId)}`, { method: 'DELETE' })
        ElementPlus.ElMessage.success('报告已删除')
        await Promise.all([loadReports(reports.page), loadDashboard()])
      } catch (err) {
        ElementPlus.ElMessage.error(err.message)
      }
    }

    async function deleteSelectedReports() {
      if (!reports.selected.length) return
      if (!(await confirmDanger(`确认删除选中的 ${reports.selected.length} 份报告吗？`))) return
      try {
        await request('/api/admin/reports/batch-delete', { method: 'POST', body: JSON.stringify({ doc_ids: reports.selected }) })
        ElementPlus.ElMessage.success('报告已批量删除')
        await Promise.all([loadReports(1), loadDashboard()])
      } catch (err) {
        ElementPlus.ElMessage.error(err.message)
      }
    }

    async function deleteUserReports() {
      if (!reports.userId) return
      if (!(await confirmDanger(`确认删除用户 ${reports.userId} 的全部报告吗？`))) return
      try {
        await request(`/api/admin/users/${encodeURIComponent(reports.userId)}/reports`, { method: 'DELETE' })
        ElementPlus.ElMessage.success('该用户报告已删除')
        await Promise.all([loadReports(1), loadDashboard()])
      } catch (err) {
        ElementPlus.ElMessage.error(err.message)
      }
    }

    async function loadSessions(page = sessions.page) {
      loading.sessions = true
      try {
        sessions.page = page
        const params = new URLSearchParams({
          user_id: sessions.userId,
          scene: sessions.scene,
          limit: sessions.pageSize,
          offset: (page - 1) * sessions.pageSize
        })
        const data = await request(`/api/admin/sessions?${params}`)
        sessions.rows = data.sessions || []
        sessions.total = data.total || 0
        sessions.selected = []
      } catch (err) {
        ElementPlus.ElMessage.error(err.message)
      } finally {
        loading.sessions = false
      }
    }

    function resetSessions() {
      sessions.userId = ''
      sessions.scene = ''
      loadSessions(1)
    }

    function onSessionSelect(rows) {
      sessions.selected = rows.map(row => row.id)
    }

    async function openSession(sessionId) {
      try {
        sessionDialog.data = await request(`/api/admin/sessions/${encodeURIComponent(sessionId)}`)
        sessionDialog.open = true
      } catch (err) {
        ElementPlus.ElMessage.error(err.message)
      }
    }

    async function deleteSession(sessionId) {
      if (!(await confirmDanger(`确认删除会话 ${sessionId} 吗？`))) return
      try {
        await request(`/api/admin/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' })
        ElementPlus.ElMessage.success('会话已删除')
        await Promise.all([loadSessions(sessions.page), loadDashboard()])
      } catch (err) {
        ElementPlus.ElMessage.error(err.message)
      }
    }

    async function deleteSelectedSessions() {
      if (!sessions.selected.length) return
      if (!(await confirmDanger(`确认删除选中的 ${sessions.selected.length} 条问诊记录吗？`))) return
      try {
        await request('/api/admin/sessions/batch-delete', { method: 'POST', body: JSON.stringify({ session_ids: sessions.selected }) })
        ElementPlus.ElMessage.success('问诊记录已批量删除')
        await Promise.all([loadSessions(1), loadDashboard()])
      } catch (err) {
        ElementPlus.ElMessage.error(err.message)
      }
    }

    async function deleteUserSessions() {
      if (!sessions.userId) return
      if (!(await confirmDanger(`确认删除用户 ${sessions.userId} 的全部问诊记录吗？`))) return
      try {
        await request(`/api/admin/users/${encodeURIComponent(sessions.userId)}/sessions`, { method: 'DELETE' })
        ElementPlus.ElMessage.success('该用户问诊记录已删除')
        await Promise.all([loadSessions(1), loadDashboard()])
      } catch (err) {
        ElementPlus.ElMessage.error(err.message)
      }
    }

    async function loadKb() {
      loading.kb = true
      try {
        const data = await request('/api/admin/kb/stats')
        kb.files = data.files || []
        kb.page = 1
      } catch (err) {
        ElementPlus.ElMessage.error(err.message)
      } finally {
        loading.kb = false
      }
    }

    function onPickKbFile(file, files = []) {
      kb.pickedFiles = (files || [file]).map(item => item.raw).filter(Boolean)
    }

    async function uploadKb() {
      if (!kb.pickedFiles.length || kb.busy) return
      kb.busy = true
      kb.progress = 0
      kb.progressText = `准备上传 ${kb.pickedFiles.length} 个文件`
      let chunks = 0
      try {
        for (let i = 0; i < kb.pickedFiles.length; i += 1) {
          const file = kb.pickedFiles[i]
          kb.progressText = `正在向量化 ${i + 1}/${kb.pickedFiles.length}：${file.name}`
          const form = new FormData()
          form.append('file', file)
          const data = await request('/api/admin/kb/upload', { method: 'POST', body: form })
          chunks += Number(data.chunks || 0)
          kb.progress = Math.round(((i + 1) / kb.pickedFiles.length) * 100)
        }
        ElementPlus.ElMessage.success(`上传完成，写入 ${chunks} 个向量片段`)
        kb.pickedFiles = []
        await Promise.all([loadKb(), loadDashboard()])
      } catch (err) {
        ElementPlus.ElMessage.error(err.message)
      } finally {
        kb.progressText = ''
        kb.busy = false
      }
    }

    async function deleteKbFile(row) {
      if (!(await confirmDanger(`确认删除知识库文件“${row.name}”及其对应向量吗？`))) return
      try {
        const data = await request(`/api/admin/kb/file?path=${encodeURIComponent(row.path)}`, { method: 'DELETE' })
        ElementPlus.ElMessage.success(`已删除文件，清理向量 ${data.vectors_deleted || 0} 条`)
        await Promise.all([loadKb(), loadDashboard()])
      } catch (err) {
        ElementPlus.ElMessage.error(err.message)
      }
    }

    async function rebuildKb() {
      if (!(await confirmDanger('确认重建全部知识库向量吗？'))) return
      kb.rebuilding = true
      kb.rebuildProgress = 5
      const timer = window.setInterval(() => {
        if (kb.rebuildProgress < 90) kb.rebuildProgress += 5
      }, 800)
      try {
        await request('/api/admin/kb/rebuild?reset=true', { method: 'POST' })
        kb.rebuildProgress = 100
        ElementPlus.ElMessage.success('知识库向量已重建')
        await Promise.all([loadKb(), loadDashboard()])
      } catch (err) {
        ElementPlus.ElMessage.error(err.message)
      } finally {
        window.clearInterval(timer)
        window.setTimeout(() => {
          kb.rebuilding = false
          kb.rebuildProgress = 0
        }, 600)
      }
    }

    async function clearKbVectors() {
      if (!(await confirmDanger('确认清空知识库向量吗？源文件不会删除。'))) return
      try {
        await request('/api/admin/kb/vectors', { method: 'DELETE' })
        ElementPlus.ElMessage.success('知识库向量已清空')
        await loadDashboard()
      } catch (err) {
        ElementPlus.ElMessage.error(err.message)
      }
    }

    async function clearReportVectors() {
      if (!(await confirmDanger('确认清空所有报告向量吗？PostgreSQL 报告记录不会删除。'))) return
      try {
        await request('/api/admin/chroma/user-reports', { method: 'DELETE' })
        ElementPlus.ElMessage.success('报告向量已清空')
        await loadDashboard()
      } catch (err) {
        ElementPlus.ElMessage.error(err.message)
      }
    }

    if (authenticated.value) loadDashboard()

    return {
      view, tabs, apiBase, authenticated, loginLoading, loginForm, loading, stats, breakdown,
      users, reports, sessions, kb, userDialog, reportDialog, sessionDialog,
      viewTitle, metrics, kbPagedFiles, sceneChartRef, docTypeChartRef, trendChartRef,
      login, logout, openView, closeTab,
      loadUsers, resetUsers, openUser, deleteUser,
      loadReports, resetReports, onReportSelect, openReport, deleteReport, deleteSelectedReports, deleteUserReports,
      loadSessions, resetSessions, onSessionSelect, openSession, deleteSession, deleteSelectedSessions, deleteUserSessions,
      loadKb, onPickKbFile, uploadKb, deleteKbFile, rebuildKb, clearKbVectors, clearReportVectors,
      text, gender, json, size, confidence, confidenceType, sceneName, sceneType
    }
  }
}).use(ElementPlus).mount('#app')
