import request from '@/utils/request'

export function getRadarMarketContext () {
  return request({
    url: '/api/radar/market-context',
    method: 'get'
  })
}
