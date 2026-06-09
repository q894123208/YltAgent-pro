import axios from 'axios'

export const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8012',
  timeout: 180000
})

export function getToken() {
  return localStorage.getItem('medical_agent_token') || ''
}

export function setAuth(token, user) {
  localStorage.setItem('medical_agent_token', token)
  localStorage.setItem('medical_agent_user', JSON.stringify(user || {}))
}

export function clearAuth() {
  localStorage.removeItem('medical_agent_token')
  localStorage.removeItem('medical_agent_user')
}

export function getStoredUser() {
  try { return JSON.parse(localStorage.getItem('medical_agent_user') || 'null') } catch { return null }
}

api.interceptors.request.use(config => {
  const token = getToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

export async function login(phone, password) {
  const { data } = await api.post('/api/auth/login', { phone, password })
  setAuth(data.token, data.user)
  return data
}

export async function register(phone, password, displayName = '', idNumber = '') {
  const { data } = await api.post('/api/auth/register', {
    phone,
    password,
    display_name: displayName,
    id_number: idNumber
  })
  setAuth(data.token, data.user)
  return data
}

export async function getMe() {
  const { data } = await api.get('/api/auth/me')
  return data.user
}

export async function updateMe(payload) {
  const { data } = await api.patch('/api/auth/me', payload)
  const token = getToken()
  setAuth(token, data.user)
  return data.user
}

// ========== 业务数据 ==========
export async function getRecords(days = 7) {
  const { data } = await api.get('/api/records', { params: { days } })
  return data.records
}

export async function getReports() {
  const { data } = await api.get('/api/reports')
  return data.reports
}

export async function getMedicalDocuments() {
  const { data } = await api.get('/api/upload/medical-documents')
  return data.documents || []
}

export async function getMedicalDocument(docId) {
  const { data } = await api.get(`/api/upload/medical-document/${docId}`)
  return data
}

export async function interpretReport(reportId) {
  const { data } = await api.get(`/api/reports/${reportId}/interpret`)
  return data
}

export async function getDepartments() {
  const { data } = await api.get('/api/departments')
  return data.departments
}

export async function getSchedule(department) {
  const { data } = await api.get('/api/appointments/schedule', { params: { department } })
  return data
}

export async function createAppointment(payload) {
  const { data } = await api.post('/api/appointments', payload)
  return data
}

export async function getAppointments() {
  const { data } = await api.get('/api/appointments')
  return data.appointments
}

export async function cancelAppointment(appointmentId) {
  const { data } = await api.delete(`/api/appointments/${appointmentId}`)
  return data
}

export async function getMetrics() {
  const { data } = await api.get('/api/metrics')
  return data
}

export async function getSessions(scene = '') {
  const { data } = await api.get('/api/sessions', { params: scene ? { scene } : {} })
  return data.sessions
}

export async function getSessionMessages(sessionId) {
  const { data } = await api.get(`/api/sessions/${sessionId}/messages`)
  return data.messages || []
}

export async function deleteSession(sessionId) {
  const { data } = await api.delete(`/api/sessions/${sessionId}`)
  return data
}

export async function getSettings() {
  const { data } = await api.get('/api/settings')
  return data
}

export async function clearAllData() {
  const { data } = await api.delete('/api/sessions')
  return data
}

export async function deleteMyReports() {
  const { data } = await api.delete('/api/upload/admin/my-reports')
  return data
}

export async function deleteAllReports() {
  const { data } = await api.delete('/api/upload/admin/all-reports')
  return data
}

// ========== 多模态附件 ==========
export async function uploadMedicalDocument(file, { sessionId = null, docTypeHint = null, note = null, onProgress } = {}) {
  const form = new FormData()
  form.append('file', file)
  if (sessionId) form.append('session_id', sessionId)
  if (docTypeHint) form.append('doc_type_hint', docTypeHint)
  if (note) form.append('note', note)
  const { data } = await api.post('/api/upload/medical-document', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 300000,
    onUploadProgress: onProgress
  })
  return data
}

export function rawDocUrl(docId) {
  return `${api.defaults.baseURL}/api/upload/medical-document/${docId}/raw?access_token=${encodeURIComponent(getToken())}`
}

/**
 * 并发上传多个文件，limit 控制并发数。
 * 返回顺序与入参一致；失败的项 ok=false + error。
 */
export async function uploadMedicalDocumentsConcurrent(files, options = {}, limit = 5) {
  const results = new Array(files.length).fill(null)
  let cursor = 0
  async function worker() {
    while (cursor < files.length) {
      const idx = cursor++
      const file = files[idx]
      try {
        const data = await uploadMedicalDocument(file, options)
        results[idx] = { ok: true, data, fileName: file.name }
      } catch (err) {
        results[idx] = { ok: false, error: err?.response?.data?.detail || err.message || '上传失败', fileName: file.name }
      }
      if (typeof options.onItemDone === 'function') options.onItemDone(idx, results[idx])
    }
  }
  const workers = Array.from({ length: Math.min(limit, files.length) }, () => worker())
  await Promise.all(workers)
  return results
}

// ========== 流式 SSE 对话 ==========
/**
 * 通用流式 SSE 客户端。
 * scene: 'triage' | 'guided_triage' | 'consultation' | 'medication' | 'medication_safety'
 * handlers: { onSession, onTrace, onEvidence, onChunk, onDone, onError }
 * 返回 { promise, abort }
 */
export function streamMedicalChat(scene, payload, handlers = {}) {
  const sceneMap = {
    triage: '/api/triage/stream',
    guided_triage: '/api/guided-triage/stream',
    consultation: '/api/consultation/stream',
    medication: '/api/medication/stream',
    medication_safety: '/api/medication-safety/agent/stream'
  }
  const url = `${api.defaults.baseURL}${sceneMap[scene]}`
  const ctrl = new AbortController()
  const promise = (async () => {
    let resp
    try {
      resp = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'text/event-stream',
          ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {})
        },
        body: JSON.stringify(payload),
        signal: ctrl.signal
      })
    } catch (err) {
      handlers.onError && handlers.onError(err)
      throw err
    }
    if (!resp.ok || !resp.body) {
      const detail = await resp.text().catch(() => '')
      const err = new Error(`HTTP ${resp.status}: ${detail || resp.statusText}`)
      handlers.onError && handlers.onError(err)
      throw err
    }
    const reader = resp.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''
    while (true) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let idx
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const raw = buffer.slice(0, idx).trim()
        buffer = buffer.slice(idx + 2)
        if (!raw.startsWith('data:')) continue
        const data = raw.slice(5).trim()
        if (data === '[DONE]') return
        let evt
        try { evt = JSON.parse(data) } catch { continue }
        switch (evt.type) {
          case 'session': handlers.onSession && handlers.onSession(evt); break
          case 'trace': handlers.onTrace && handlers.onTrace(evt); break
          case 'evidence': handlers.onEvidence && handlers.onEvidence(evt); break
          case 'chunk': handlers.onChunk && handlers.onChunk(evt.delta || ''); break
          case 'phase': handlers.onPhase && handlers.onPhase(evt); break
          case 'done': handlers.onDone && handlers.onDone(evt); break
          case 'error': handlers.onError && handlers.onError(new Error(evt.detail || 'stream error')); break
        }
      }
    }
  })()
  return { promise, abort: () => ctrl.abort() }
}
