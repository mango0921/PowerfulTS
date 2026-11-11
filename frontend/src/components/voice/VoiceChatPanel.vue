<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, nextTick } from 'vue'
import apiClient from '@/api/client'

interface ChatMessage {
  id: number
  name: string
  text: string
  at: number
  self?: boolean
  private?: boolean
}

const messages = ref<ChatMessage[]>([])
const draft = ref('')
const status = ref<'idle' | 'connecting' | 'live' | 'unavailable'>('idle')
const hint = ref('')
const listEl = ref<HTMLElement | null>(null)

let socket: WebSocket | null = null
let retryTimer: number | null = null
let disposed = false
let seq = 0

function scrollToBottom() {
  void nextTick(() => {
    if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight
  })
}

function pushMessage(message: Omit<ChatMessage, 'id'>) {
  messages.value = [...messages.value, { ...message, id: ++seq }].slice(-200)
  scrollToBottom()
}

function clearRetry() {
  if (retryTimer !== null) {
    window.clearTimeout(retryTimer)
    retryTimer = null
  }
}

function scheduleRetry(delay = 5000) {
  clearRetry()
  if (disposed) return
  retryTimer = window.setTimeout(() => {
    retryTimer = null
    void connect()
  }, delay)
}

async function connect() {
  if (disposed || socket) return
  status.value = status.value === 'live' ? 'connecting' : status.value || 'connecting'
  try {
    const { data } = await apiClient.post<{ streamPath: string }>('/music/voice/chat/start')
    if (disposed) return
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${scheme}://${window.location.host}${data.streamPath}`)
    socket = ws
    ws.onopen = () => {
      status.value = 'live'
      hint.value = ''
    }
    ws.onmessage = (event) => {
      try {
        const payload = JSON.parse(String(event.data)) as {
          type?: string
          invokerName?: string
          message?: string
          at?: number
          targetMode?: number
        }
        if (payload.type !== 'message' || !payload.message) return
        pushMessage({
          name: payload.invokerName || '未知用户',
          text: payload.message,
          at: payload.at || Date.now(),
          private: payload.targetMode === 1,
        })
      } catch {
        /* 非 JSON 帧忽略 */
      }
    }
    ws.onclose = () => {
      socket = null
      if (disposed) return
      status.value = 'connecting'
      hint.value = '聊天连接已断开，正在重连…'
      scheduleRetry(3000)
    }
    ws.onerror = () => ws.close()
  } catch (error) {
    socket = null
    status.value = 'unavailable'
    const status_ = (error as { response?: { status?: number } })?.response?.status
    hint.value = status_ === 409
      ? '先加入通话，才能收发频道聊天'
      : '聊天暂不可用，稍后自动重试'
    scheduleRetry(6000)
  }
}

function send() {
  const text = draft.value.trim()
  if (!text || !socket || socket.readyState !== WebSocket.OPEN) return
  socket.send(JSON.stringify({ type: 'send', text }))
  pushMessage({ name: '我', text, at: Date.now(), self: true })
  draft.value = ''
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    send()
  }
}

function formatTime(at: number) {
  const date = new Date(at)
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`
}

/** 聊天里的图片：裸图片链接 + TeamSpeak 的 [img]…[/img] 两种写法。 */
const IMAGE_PATTERN = /\[img\]\s*(https?:\/\/[^\s[\]]+)\s*\[\/img\]|https?:\/\/[^\s[\]]+?\.(?:gif|png|jpe?g|webp)(?:\?[^\s[\]]*)?/gi

interface MessagePart {
  type: 'text' | 'image' | 'file'
  value: string
  name?: string
  size?: number
}

type TsFileEnvelope = {
  msg_type?: string
  body?: string
  file_name?: string
  file_size?: number
}

/** TS6 的文件消息以 JSON 文本下发（msg_type=ts.file.*），解析成附件卡片。 */
function parseTsEnvelope(text: string): { kind: 'file'; name: string; size?: number } | { kind: 'text'; body: string } | null {
  const trimmed = text.trim()
  if (!trimmed.startsWith('{') || !trimmed.endsWith('}')) return null
  let payload: TsFileEnvelope
  try {
    payload = JSON.parse(trimmed) as TsFileEnvelope
  } catch {
    return null
  }
  if (typeof payload.msg_type === 'string' && payload.msg_type.startsWith('ts.file')) {
    return {
      kind: 'file',
      name: payload.file_name || payload.body || '文件',
      size: typeof payload.file_size === 'number' ? payload.file_size : undefined,
    }
  }
  if (typeof payload.body === 'string') return { kind: 'text', body: payload.body }
  return null
}

function formatSize(size?: number): string {
  if (!size || size <= 0) return ''
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}

function messageParts(text: string): MessagePart[] {
  const envelope = parseTsEnvelope(text)
  if (envelope?.kind === 'file') {
    return [{ type: 'file', value: envelope.name, name: envelope.name, size: envelope.size }]
  }
  const body = envelope?.kind === 'text' ? envelope.body : text
  const parts: MessagePart[] = []
  let last = 0
  for (const match of body.matchAll(IMAGE_PATTERN)) {
    const index = match.index ?? 0
    if (index > last) parts.push({ type: 'text', value: body.slice(last, index) })
    const url = match[1] || match[0]
    parts.push({ type: 'image', value: url })
    last = index + match[0].length
  }
  if (last < body.length) parts.push({ type: 'text', value: body.slice(last) })
  return parts.length ? parts : [{ type: 'text', value: body }]
}

function hideBrokenImage(event: Event) {
  const img = event.target as HTMLImageElement | null
  if (!img) return
  // 加载失败就退回成链接文本，避免留一个破图标。
  const link = document.createElement('a')
  link.href = img.src
  link.textContent = img.src
  link.target = '_blank'
  link.rel = 'noopener noreferrer'
  link.className = 'chat-image-fallback'
  img.replaceWith(link)
}

onMounted(() => { void connect() })
onBeforeUnmount(() => {
  disposed = true
  clearRetry()
  socket?.close(1000, 'panel closed')
  socket = null
})
</script>

<template>
  <section class="voice-chat panel-surface">
    <header class="chat-head">
      <span class="chat-status" :class="status">
        {{ status === 'live' ? '已连接' : status === 'connecting' ? '连接中…' : '未连接' }}
      </span>
    </header>

    <div ref="listEl" class="chat-list">
      <p v-if="messages.length === 0" class="chat-empty">
        {{ hint || '还没有消息，和频道里的朋友打个招呼吧' }}
      </p>
      <div v-for="message in messages" :key="message.id" class="chat-row" :class="{ self: message.self }">
        <span class="chat-time mono">{{ formatTime(message.at) }}</span>
        <span class="chat-name">{{ message.name }}</span>
        <span v-if="message.private" class="chat-badge">私聊</span>
        <span class="chat-text">
          <template v-for="(part, index) in messageParts(message.text)" :key="index">
            <span v-if="part.type === 'file'" class="chat-file">
              <span class="chat-file-name">📎 {{ part.name }}</span>
              <span v-if="part.size" class="chat-file-size">{{ formatSize(part.size) }}</span>
              <span class="chat-file-hint">文件消息暂不支持预览</span>
            </span>
            <img
              v-else-if="part.type === 'image'"
              :src="part.value"
              class="chat-image"
              loading="lazy"
              referrerpolicy="no-referrer"
              alt="聊天图片"
              @error="hideBrokenImage"
            />
            <template v-else>{{ part.value }}</template>
          </template>
        </span>
      </div>
    </div>

    <form class="chat-input" @submit.prevent="send">
      <el-input
        v-model="draft"
        maxlength="400"
        :disabled="status !== 'live'"
        placeholder="发送到当前频道（Enter 发送）"
        @keydown="onKeydown"
      />
      <button type="submit" class="chat-send" :disabled="status !== 'live' || !draft.trim()">发送</button>
    </form>
  </section>
</template>

<style scoped>
.voice-chat {
  display: flex;
  flex-direction: column;
  gap: 10px;
  /* 与「一起看」「入场音效」保持一致的面板样式 */
  padding: 18px 20px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  box-shadow: var(--shadow-card);
  min-width: 0;
  min-height: 180px;
  max-height: 420px;
}
.chat-head {
  display: flex;
  align-items: center;
  justify-content: flex-end;
}
.chat-head h3 {
  margin: 0;
  font-size: 0.92em;
}
.chat-status {
  font-size: 0.72rem;
  color: var(--text-muted);
}
.chat-status.live { color: #4ade80; }
.chat-list {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 0.82rem;
  line-height: 1.45;
}
.chat-empty {
  color: var(--text-muted);
  margin: 0;
  font-size: 0.8rem;
}
.chat-row {
  display: flex;
  gap: 8px;
  align-items: baseline;
  word-break: break-word;
}
.chat-time {
  color: var(--text-muted);
  font-size: 0.7rem;
  flex: none;
}
.chat-name {
  color: #7dd3fc;
  flex: none;
}
.chat-row.self .chat-name { color: #4ade80; }
.chat-badge {
  font-size: 0.62rem;
  padding: 1px 5px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.1);
  color: var(--text-muted);
  flex: none;
}
.chat-text { color: var(--text-primary); }
.chat-image {
  display: block;
  max-width: min(100%, 260px);
  max-height: 200px;
  margin: 4px 0;
  border-radius: 8px;
  border: 1px solid var(--border-subtle);
  object-fit: contain;
  background: var(--bg-elevated);
}
.chat-image-fallback {
  color: #7dd3fc;
  text-decoration: underline;
  word-break: break-all;
}
.chat-file {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin: 3px 0;
  padding: 6px 10px;
  border: 1px dashed var(--border-subtle);
  border-radius: 8px;
  background: var(--bg-elevated);
  font-size: 0.95em;
}
.chat-file-name { color: var(--text-primary); }
.chat-file-size { color: var(--text-muted); font-size: 0.85em; }
.chat-file-hint { color: var(--text-muted); font-size: 0.8em; }
.chat-input {
  display: flex;
  gap: 8px;
  align-items: center;
}
.chat-send {
  flex: none;
  padding: 7px 14px;
  border-radius: 8px;
  border: 1px solid rgba(125, 211, 252, 0.35);
  background: rgba(125, 211, 252, 0.12);
  color: #bae6fd;
  cursor: pointer;
}
.chat-send:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.chat-hint {
  margin: 0;
  font-size: 0.72rem;
  color: var(--text-muted);
}
</style>
