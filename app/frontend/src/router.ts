import { createRouter, createWebHistory } from 'vue-router'
import { auth } from './api/client'
import Layout from './views/Layout.vue'
import Login from './views/Login.vue'
import Placeholder from './views/Placeholder.vue'
import Ops from './views/Ops.vue'
import Overview from './views/Overview.vue'

export const NAV = [
  { path: '/', name: 'overview', label: '总览' },
  { path: '/market', name: 'market', label: '行情' },
  { path: '/research', name: 'research', label: '研究' },
  { path: '/signals', name: 'signals', label: '信号' },
  { path: '/reports', name: 'reports', label: '报告' },
  { path: '/ops', name: 'ops', label: '运维' },
]

export const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/login', name: 'login', component: Login, meta: { public: true } },
    {
      path: '/', component: Layout, children: [
        { path: '', name: 'overview', component: Overview },
        { path: 'market', name: 'market', component: Placeholder, props: { title: '行情' } },
        { path: 'research', name: 'research', component: Placeholder, props: { title: '研究' } },
        { path: 'signals', name: 'signals', component: Placeholder, props: { title: '信号' } },
        { path: 'reports', name: 'reports', component: Placeholder, props: { title: '报告' } },
        { path: 'ops', name: 'ops', component: Ops },
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
