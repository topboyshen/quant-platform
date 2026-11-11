export const PERSONAL_HOME_PATH = '/personal-home'

// Retired commercial/multi-user pages. Keep these aliases so old bookmarks
// cannot fall through to a 404 page or survive as a post-login redirect.
export const RETIRED_PERSONAL_ROUTE_PATHS = Object.freeze([
  '/billing',
  '/broker-accounts',
  '/indicator-community',
  '/portfolio',
  '/user-manage',
  '/credentials',
  '/community',
  '/account',
  '/bots',
  '/quick-trade',
  '/executor-strategies',
  '/executors',
  '/live-strategies',
  '/marketplace',
  '/subscriptions'
])

function decodedInternalPath (value) {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) {
    return null
  }

  try {
    return decodeURIComponent(value)
  } catch (error) {
    return null
  }
}

export function isRetiredPersonalRoute (value) {
  const decoded = decodedInternalPath(value)
  if (!decoded) return false

  const pathname = decoded.split(/[?#]/, 1)[0].replace(/\/+$/, '') || '/'
  const normalizedPath = pathname.toLowerCase()
  return RETIRED_PERSONAL_ROUTE_PATHS.some(path => (
    normalizedPath === path || normalizedPath.startsWith(`${path}/`)
  ))
}

export function normalizePersonalRedirect (value, fallback = PERSONAL_HOME_PATH) {
  const decoded = decodedInternalPath(value)
  if (!decoded || isRetiredPersonalRoute(decoded)) {
    return fallback
  }
  return decoded
}
