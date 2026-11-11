<template>
  <div class="ai-asset-analysis-page" :class="{ 'is-dark': isDarkTheme }">
    <copilot-workbench personal-mode />
  </div>
</template>

<script>
import { mapState } from 'vuex'
import CopilotWorkbench from '@/views/ai-analysis/components/CopilotWorkbench.vue'

export default {
  name: 'AIAssetAnalysis',
  components: { CopilotWorkbench },
  computed: {
    ...mapState({ navTheme: state => state.app.theme }),
    isDarkTheme () {
      return this.navTheme === 'dark' || this.navTheme === 'realdark'
    }
  }
}
</script>

<style lang="less" scoped>
.ai-asset-analysis-page {
  height: calc(100vh - 64px);
  min-height: 640px;
}
</style>
