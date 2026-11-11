<template>
  <nav class="workspace-navigation" :class="{ 'workspace-navigation--mobile': mobile }" aria-label="全站导航">
    <div
      v-for="group in groups"
      :key="group.id"
      class="workspace-navigation__group"
      :class="{ 'workspace-navigation__group--active': isActive(group), 'workspace-navigation__group--operations': group.id === 'operations' }"
    >
      <template v-if="group.children">
        <button
          type="button"
          class="workspace-navigation__heading"
          :aria-expanded="isExpanded(group) ? 'true' : 'false'"
          :aria-controls="`${idPrefix}-${group.id}`"
          @click="toggleGroup(group)"
        >
          <a-icon :type="group.icon" aria-hidden="true" />
          <span>{{ group.label }}</span>
          <a-icon class="workspace-navigation__chevron" :type="isExpanded(group) ? 'up' : 'down'" aria-hidden="true" />
        </button>
        <div v-show="isExpanded(group)" :id="`${idPrefix}-${group.id}`" class="workspace-navigation__children">
          <router-link
            v-for="item in group.children"
            :key="item.id"
            :to="target(item)"
            class="workspace-navigation__child"
            :class="{ 'workspace-navigation__link--active': isActive(item) }"
            :aria-current="isActive(item) ? 'page' : null"
            @click.native="notifyNavigation(item)"
          >{{ item.label }}</router-link>
        </div>
      </template>
      <router-link
        v-else
        :to="target(group)"
        class="workspace-navigation__heading"
        :class="{ 'workspace-navigation__link--active': isActive(group) }"
        :aria-current="isActive(group) ? 'page' : null"
        @click.native="notifyNavigation(group)"
      >
        <a-icon :type="group.icon" aria-hidden="true" />
        <span>{{ group.label }}</span>
      </router-link>
    </div>
  </nav>
</template>

<script>
import { isWorkspaceItemActive, workspaceNavigationTarget } from '@/utils/workspaceNavigation'

export default {
  name: 'WorkspaceNavigation',
  props: {
    groups: { type: Array, default: () => [] },
    route: { type: Object, required: true },
    mobile: { type: Boolean, default: false }
  },
  data () {
    return { expanded: {} }
  },
  computed: {
    idPrefix () {
      return this.mobile ? 'workspace-mobile' : 'workspace-sidebar'
    }
  },
  watch: {
    'route.fullPath' () {
      this.groups.forEach(group => {
        if (group.children && this.isActive(group)) this.$set(this.expanded, group.id, true)
      })
    }
  },
  methods: {
    isActive (item) {
      return isWorkspaceItemActive(item, this.route)
    },
    target (item) {
      return workspaceNavigationTarget(item, this.route)
    },
    isExpanded (group) {
      if (Object.prototype.hasOwnProperty.call(this.expanded, group.id)) return this.expanded[group.id]
      return this.isActive(group) || (!this.mobile && group.id === 'research')
    },
    toggleGroup (group) {
      this.$set(this.expanded, group.id, !this.isExpanded(group))
    },
    notifyNavigation (item) {
      this.$emit('navigate', item.to)
    }
  }
}
</script>

<style scoped>
.workspace-navigation {
  display: flex;
  flex-direction: column;
  gap: 7px;
  min-width: 0;
  padding: 22px 12px;
  color: inherit;
}
.workspace-navigation__heading,
.workspace-navigation__child {
  display: flex;
  align-items: center;
  min-height: 44px;
  gap: 10px;
  padding: 11px 13px;
  border: 0;
  border-radius: 8px;
  color: inherit;
  background: transparent;
  text-align: left;
  text-decoration: none;
  line-height: 1.45;
  cursor: pointer;
}
.workspace-navigation__heading { width: 100%; font: inherit; font-weight: 500; }
.workspace-navigation__heading > span { flex: 1; }
.workspace-navigation__heading:hover,
.workspace-navigation__child:hover { background: var(--workspace-nav-hover, rgba(100, 116, 139, .12)); }
.workspace-navigation__heading:focus-visible,
.workspace-navigation__child:focus-visible { outline: 2px solid var(--primary-color, #52c41a); outline-offset: 2px; }
.workspace-navigation__group--active > .workspace-navigation__heading { color: var(--workspace-nav-accent, var(--primary-color, #52c41a)); }
.workspace-navigation__link--active { color: var(--workspace-nav-accent, var(--primary-color, #52c41a)); background: var(--workspace-nav-selected, rgba(82, 196, 26, .12)); font-weight: 600; }
.workspace-navigation__children { display: grid; gap: 3px; margin: 5px 0 2px 20px; padding-left: 8px; border-left: 1px solid var(--workspace-nav-line, rgba(148, 163, 184, .22)); }
.workspace-navigation__child { font-size: 13px; padding: 9px 10px; }
.workspace-navigation__chevron { font-size: 10px; opacity: .65; }
.workspace-navigation__group--operations { border-top: 1px solid var(--workspace-nav-line, rgba(148, 163, 184, .22)); padding-top: 14px; margin-top: 14px; }
.workspace-navigation--mobile { flex-flow: row wrap; gap: 5px; padding: 12px 14px; border-bottom: 1px solid var(--workspace-nav-line, rgba(148, 163, 184, .22)); }
.workspace-navigation--mobile .workspace-navigation__group { min-width: 0; }
.workspace-navigation--mobile .workspace-navigation__heading { min-height: 44px; padding: 9px 10px; font-size: 13px; gap: 7px; }
.workspace-navigation--mobile .workspace-navigation__group--operations { border: 0; padding: 0; margin: 0; }
.workspace-navigation--mobile .workspace-navigation__children { margin-left: 0; padding-left: 0; border-left: 0; }
</style>
