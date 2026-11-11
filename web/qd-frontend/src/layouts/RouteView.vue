<script lang="jsx">
export default {
  name: 'RouteView',
  props: {
    keepAlive: {
      type: Boolean,
      default: true
    }
  },
  data () {
    return {}
  },
  render () {
    const { $route } = this
    // Query-only changes belong to the same workspace instance. Using
    // `fullPath` here used to destroy editors and AI drafts whenever a route
    // added or removed a query parameter.
    const matched = Array.isArray($route.matched) ? $route.matched[$route.matched.length - 1] : null
    const routeKey = $route.name || (matched && matched.path) || $route.path
    // Keep the cache owner mounted even while the active route opts out. If
    // the <keep-alive> root itself is conditional, visiting Settings destroys
    // every cached research draft before the user returns.
    return (
      <div class="route-view-cache-host">
        <keep-alive>
          {this.keepAlive ? <router-view key={routeKey} /> : null}
        </keep-alive>
        {!this.keepAlive ? <router-view key={routeKey} /> : null}
      </div>
    )
  }
}
</script>

<style scoped>
.route-view-cache-host {
  min-height: 100%;
}
</style>
