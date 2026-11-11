<template>
  <div class="rl2-draftbar" :data-testid="'rl2-draftbar-' + module">
    <span :class="['rl2-state', 'rl2-state--' + label.tone]">{{ label.text }}</span>
    <a-button size="small" :disabled="disabled" :data-testid="'rl2-draft-save-' + module" @click="$emit('save')">保存草稿（写入服务端）</a-button>
    <a-button v-if="draft" size="small" :disabled="disabled" :data-testid="'rl2-draft-clear-' + module" @click="$emit('clear')">清除草稿</a-button>
    <a-button v-if="draft && dirty" size="small" :disabled="disabled" @click="$emit('restore')">载入服务端草稿</a-button>
  </div>
</template>

<script>
import { saveStateLabel } from './workspaceLogic'

export default {
  name: 'DraftBar',
  props: { draft: { type: Object, default: null }, dirty: Boolean, disabled: Boolean, module: { type: String, required: true } },
  computed: {
    label () { return saveStateLabel({ dirty: this.dirty, draft: this.draft }) }
  }
}
</script>

<style lang="less" scoped>
.rl2-draftbar { display: flex; gap: 8px; align-items: center; margin-top: 8px; padding-top: 8px; border-top: 1px dashed #eee; flex-wrap: wrap; }
.rl2-state--warn { color: #d46b08; font-weight: 600; }
.rl2-state--ok { color: #389e0d; font-weight: 600; }
.rl2-state--info { color: #1677ff; font-weight: 600; }
.rl2-state--muted { color: #999; }
</style>
