import { createRouter, createWebHistory } from 'vue-router'
import { auth } from './api/client'
import Layout from './views/Layout.vue'
import Login from './views/Login.vue'

// Route-level code splitting: every page (and ECharts) is a lazy chunk.

export const NAV = [
  { path: '/', name: 'overview', label: '总览' },
  { path: '/market', name: 'market', label: '行情' },
  { path: '/research', name: 'research', label: '研究' },
  { path: '/signals', name: 'signals', label: '信号' },
  { path: '/reports', name: 'reports', label: '报告' },
  { path: '/ops', name: 'ops', label: '运维' },
  { path: '/settings', name: 'settings', label: '设置' },
]

export const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/login', name: 'login', component: Login, meta: { public: true } },
    {
      path: '/', component: Layout, children: [
        { path: '', name: 'overview', component: () => import('./views/Overview.vue') },
        {
          path: 'market', component: () => import('./views/market/MarketLayout.vue'), children: [
            { path: '', name: 'market', component: () => import('./views/market/Health.vue') },
            { path: 'instruments', name: 'market-instruments', component: () => import('./views/market/Instruments.vue') },
            { path: 'calendar', name: 'market-calendar', component: () => import('./views/market/Calendar.vue') },
            { path: 'index-close', name: 'market-indexclose', component: () => import('./views/market/IndexClose.vue') },
            { path: 'coverage', name: 'market-coverage', component: () => import('./views/market/MarketCoverage.vue') },
            { path: 'imports', name: 'market-imports', component: () => import('./views/market/Imports.vue') },
          ],
        },
        {
          path: 'research', component: () => import('./views/research/ResearchLayout.vue'), children: [
            { path: '', name: 'research-backtests', component: () => import('./views/research/Backtests.vue') },
            { path: 'profit', name: 'research-profit', component: () => import('./views/research/Profit.vue') },
            { path: 'ai', name: 'research-ai', component: () => import('./views/research/Ai.vue') },
            { path: 'projects', name: 'research-projects', component: () => import('./views/research/Projects.vue') },
          ],
        },
        {
          path: 'signals', component: () => import('./views/signals/SignalsLayout.vue'), children: [
            { path: '', name: 'signals-runs', component: () => import('./views/signals/Runs.vue') },
            { path: 'trends', name: 'signals-trends', component: () => import('./views/signals/Trends.vue') },
            { path: 'outcomes', name: 'signals-outcomes', component: () => import('./views/signals/Outcomes.vue') },
            { path: 'compare', name: 'signals-compare', component: () => import('./views/signals/Compare.vue') },
          ],
        },
        {
          path: 'reports', component: () => import('./views/reports/ReportsLayout.vue'), children: [
            { path: '', name: 'reports-center', component: () => import('./views/reports/Center.vue') },
            { path: 'deliveries', name: 'reports-deliveries', component: () => import('./views/reports/Deliveries.vue') },
          ],
        },
        { path: 'settings', name: 'settings', component: () => import('./views/Settings.vue') },
        {
          path: 'ops', component: () => import('./views/OpsLayout.vue'), children: [
            { path: '', name: 'ops', component: () => import('./views/Ops.vue') },
            { path: 'monitors', name: 'ops-monitors', component: () => import('./views/Monitors.vue') },
            { path: 'tasks', name: 'ops-tasks', component: () => import('./views/Tasks.vue') },
            { path: 'alerts', name: 'ops-alerts', component: () => import('./views/Alerts.vue') },
            { path: 'compare', name: 'ops-compare', component: () => import('./views/Compare.vue') },
            { path: 'services', name: 'ops-services', component: () => import('./views/Services.vue') },
            { path: 'coverage', name: 'ops-coverage', component: () => import('./views/Coverage.vue') },
            { path: 'tokens', name: 'ops-tokens', component: () => import('./views/Tokens.vue') },
          ],
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

let authed: boolean | null = null
export function setAuthed(v: boolean | null) { authed = v }

router.beforeEach(async (to) => {
  if (to.meta.public) return true
  if (authed === null) {
    try { await auth.me(); authed = true } catch { authed = false }
  }
  return authed ? true : { name: 'login', query: { redirect: to.fullPath } }
})
