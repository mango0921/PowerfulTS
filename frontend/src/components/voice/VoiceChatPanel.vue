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
        <span class="chat-text">{{ message.text }}</span>
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
