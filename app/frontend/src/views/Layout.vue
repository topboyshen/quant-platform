<template>
  <n-layout style="height: 100%">
    <n-layout-header bordered class="top">
      <span class="brand">Quant Platform</span>
      <n-menu mode="horizontal" :value="active" :options="menu" @update:value="go" />
      <span class="user">{{ me?.name }}</span>
      <n-button size="small" quaternary @click="logout">退出</n-button>
    </n-layout-header>
    <n-layout-content content-style="padding: 20px;">
      <router-view />
    </n-layout-content>
  </n-layout>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NLayout, NLayoutContent, NLayoutHeader, NMenu } from 'naive-ui'
import { auth, type Me } from '../api/client'
import { NAV, setAuthed } from '../router'

const route = useRoute(), router = useRouter()
const me = ref<Me | null>(null)
const menu = NAV.map((n) => ({ key: n.name, label: n.label }))
const active = computed(() => { const n = String(route.name || 'overview'); return n.split('-')[0] })
const DEFAULT_CHILD: Record<string, string> = {
  research: 'research-backtests',
  signals: 'signals-runs',
  reports: 'reports-center',
}
function go(key: string) { router.push({ name: DEFAULT_CHILD[key] ?? key }) }
async function logout() {
  try { await auth.logout() } finally { setAuthed(false); router.replace({ name: 'login' }) }
}
onMounted(async () => { try { me.value = await auth.me() } catch { /* guard handles */ } })
</script>
<style scoped>
.top { display: flex; align-items: center; gap: 16px; padding: 0 20px; height: 56px; }
.brand { font-weight: 600; font-size: 16px; white-space: nowrap; }
.user { margin-left: auto; color: #666; font-size: 13px; }
</style>
