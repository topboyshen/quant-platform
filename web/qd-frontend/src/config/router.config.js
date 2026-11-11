// Personal single-admin route map.
import { UserLayout, BasicLayout } from '@/layouts'
import { PERSONAL_HOME_PATH, RETIRED_PERSONAL_ROUTE_PATHS } from '@/utils/personalRoutes'

const RESEARCH_LAB_V2_ROUTES = [{
  path: '/research-lab',
  name: 'ResearchLabV2',
  component: () => import('@/views/research-lab-v2'),
  meta: { title: '研究实验室 V2', keepAlive: false, icon: 'experiment', permission: ['dashboard'] }
}]

export const asyncRouterMap = [
  {
    path: '/',
    name: 'index',
    component: BasicLayout,
    meta: { title: 'menu.home' },
    redirect: PERSONAL_HOME_PATH,
    children: [
      {
        path: '/personal-home',
        name: 'PersonalHome',
        component: () => import('@/views/personal-home'),
        meta: { title: 'Radar 决策中心', keepAlive: true, icon: 'radar-chart', permission: ['dashboard'] }
      },
      {
        path: '/personal-reports',
        name: 'PersonalReports',
        component: () => import('@/views/personal-reports'),
        meta: { title: '报告中心', keepAlive: true, icon: 'file-text', permission: ['dashboard'] }
      },
      {
        path: '/ai-asset-analysis',
        name: 'AIAssetAnalysis',
        component: () => import('@/views/ai-asset-analysis'),
        meta: { title: 'AI 资产分析', keepAlive: true, icon: 'thunderbolt', permission: ['dashboard'] }
      },
      {
        path: '/indicator-ide',
        name: 'IndicatorIDE',
        component: () => import('@/views/indicator-ide'),
        meta: { title: '指标研究', keepAlive: true, icon: 'line-chart', permission: ['dashboard'] }
      },
      {
        path: '/strategy-ide',
        name: 'StrategyIDE',
        component: () => import('@/views/strategy-ide'),
        meta: { title: '策略研究', keepAlive: true, icon: 'code', permission: ['dashboard'] }
      },
      {
        path: '/backtest-center',
        name: 'BacktestCenter',
        component: () => import('@/views/backtest-center'),
        meta: { title: '回测验证', keepAlive: true, icon: 'bar-chart', permission: ['dashboard'] }
      },
      // Research Lab V2 unified workspace: registered only when the build-time flag is on; the page
      // additionally checks GET /api/research-lab/v2/capabilities and falls back to the four legacy pages.
      ...(process.env.VUE_APP_RESEARCH_LAB_V2 === '1' ? RESEARCH_LAB_V2_ROUTES : []),
      {
        path: '/profile',
        name: 'Profile',
        component: () => import('@/views/personal-profile'),
        hidden: true,
        meta: { title: '个人设置', keepAlive: false, icon: 'user', permission: ['dashboard'] }
      },
      {
        path: '/agent-tokens',
        name: 'AgentTokens',
        component: () => import('@/views/agent-tokens'),
        meta: { title: '研究 Agent', keepAlive: false, icon: 'api', permission: ['admin'] }
      },
      {
        path: '/settings',
        name: 'Settings',
        component: () => import('@/views/settings'),
        meta: { title: '系统设置', keepAlive: false, icon: 'setting', permission: ['admin'] }
      },
      {
        path: '/dashboard',
        redirect: '/personal-home',
        hidden: true
      },
      {
        path: '/strategy-center',
        redirect: '/strategy-ide',
        hidden: true
      },
      ...RETIRED_PERSONAL_ROUTE_PATHS.map(path => ({
        path,
        redirect: PERSONAL_HOME_PATH,
        hidden: true
      })),
      {
        path: '/indicator-analysis',
        redirect: '/indicator-ide',
        hidden: true
      },
      {
        path: '/ai-analysis/:pageNo([1-9]\\d*)?',
        redirect: '/ai-asset-analysis',
        hidden: true,
        meta: { title: 'AI 分析', permission: ['dashboard'] }
      },
      {
        path: '/universe-manager',
        redirect: PERSONAL_HOME_PATH,
        hidden: true
      }
    ]
  },
  { path: '*', redirect: '/404', hidden: true }
]

export const constantRouterMap = [
  {
    path: '/strategy-runtime',
    hidden: true,
    redirect: to => ({
      path: '/strategy-ide',
      query: to.query && to.query.strategy_id ? { sourceId: to.query.strategy_id } : {}
    })
  },
  {
    path: '/user',
    component: UserLayout,
    redirect: '/user/login',
    hidden: true,
    children: [
      {
        path: 'login',
        name: 'login',
        component: () => import('@/views/user/Login')
      }
    ]
  },
  {
    path: '/404',
    meta: { title: 'menu.exception.not-find' },
    component: () => import('@/views/exception/404')
  }
]
