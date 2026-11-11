import request from '@/utils/request'

export function getWorkbenchOverview () {
  return request({
    url: '/api/radar/workbench/overview',
    method: 'get'
  })
}

export function getWorkbenchReports () {
  return request({
    url: '/api/radar/workbench/reports',
    method: 'get'
  })
}

export function getNng8Readonly () {
  return request({
    url: '/api/radar/workbench/nng8',
    method: 'get',
    nng8CallerHandlesAuth: true
  })
}
