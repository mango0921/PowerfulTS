<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useMusicStore } from '@/stores/music'

const store = useMusicStore()
const router = useRouter()
const loading = ref(false)

const bots = computed(() => store.bots)
const channelRequired = computed(() => store.channelRequired)
const detail = computed(() => store.channelDetail)
const channelName = computed(() => store.channelName)
const active = computed(
  () => bots.value.find((b) => b.id === store.activeBotId) || bots.value[0] || null,
)

async function load() {
  loading.value = true
  try {
    await store.fetchBots()
  } finally {
    loading.value = false
  }
}

function isOnline(bot: { connected?: boolean; status?: string }): boolean {
  return !!bot.connected || bot.status === 'connected'
}

function switchBot(id: string) {
  store.setActiveBot(id)
}

function goVoice() {
  router.push('/')
}

let pollTimer: ReturnType<typeof setInterval> | null = null
onMounted(() => {
  load()
  // 机器人上线/掉线、换频道都要及时反映，5 秒轮询一次状态。
  pollTimer = setInterval(load, 5000)
})
onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
defineExpose({ load })
</script>

<template>
  <section class="channel-bot panel">
    <header class="cb-head">
      <div>
        <span class="cb-label label-mono">CHANNEL BOT</span>
        <h2 class="cb-title">当前频道机器人</h2>
      </div>
      <el-button text :loading="loading" @click="load">刷新</el-button>
    </header>

    <div v-if="channelRequired" class="cb-empty">
      <p>{{ detail || '请先进入网页通话，才能控制所在频道的音乐机器人。' }}</p>
      <el-button type="primary" @click="goVoice">去网页通话</el-button>
    </div>

    <template v-else>
      <div class="cb-channel">
        <span class="label-mono">频道</span>
        <strong>{{ channelName || '—' }}</strong>
      </div>
      <div class="cb-bots">
        <button
          v-for="bot in bots"
          :key="bot.id"
          class="cb-bot"
          :class="{ active: bot.id === active?.id }"
          @click="switchBot(bot.id)"
        >
          <span class="cb-dot" :class="{ on: isOnline(bot) }"></span>
          <span class="cb-name">{{ bot.name }}</span>
          <span class="cb-state label-mono">
            {{ !isOnline(bot) ? '离线' : (bot.playing ? (bot.currentSong?.name || '播放中') : '待播放') }}
          </span>
        </button>
      </div>
      <p v-if="!bots.length" class="cb-hint">
        当前频道还没有音乐机器人，管理员在 MusicBot 后台派遣后这里会自动出现。
      </p>
    </template>
  </section>
</template>

<style scoped>
.channel-bot {
  padding: 16px 18px;
  margin-bottom: 16px;
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  background: var(--surface-2, rgba(255, 255, 255, 0.02));
}
.cb-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px;
}
.cb-label {
  font-size: 0.7em;
  letter-spacing: 0.14em;
  color: var(--text-muted);
}
.cb-title {
  margin: 4px 0 0;
  font-size: 1.05em;
}
.cb-channel {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-top: 10px;
  font-size: 0.9em;
}
.cb-bots {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}
.cb-bot {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: 10px;
  border: 1px solid var(--border-subtle);
  background: transparent;
  color: inherit;
  cursor: pointer;
}
.cb-bot.active {
  border-color: var(--accent, #4ea1ff);
  box-shadow: 0 0 0 1px var(--accent, #4ea1ff) inset;
}
.cb-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #6b7280;
}
.cb-dot.on {
  background: #22c55e;
}
.cb-state {
  font-size: 0.72em;
  color: var(--text-muted);
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cb-empty {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  align-items: flex-start;
}
.cb-hint {
  margin: 10px 0 0;
  font-size: 0.85em;
  color: var(--text-muted);
}
</style>
