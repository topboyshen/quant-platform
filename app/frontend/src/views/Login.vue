<template>
  <div class="wrap">
    <n-card title="Quant Platform 登录" style="width: 360px">
      <p class="hint">使用 QuantDinger 账号登录</p>
      <n-form @submit.prevent="submit">
        <n-form-item label="用户名"><n-input v-model:value="username" autocomplete="username" /></n-form-item>
        <n-form-item label="密码">
          <n-input v-model:value="password" type="password" show-password-on="click" autocomplete="current-password"
                   @keyup.enter="submit" />
        </n-form-item>
        <n-button type="primary" block :loading="loading" @click="submit">登录</n-button>
      </n-form>
      <n-alert v-if="error" type="error" style="margin-top: 12px">{{ error }}</n-alert>
    </n-card>
  </div>
</template>
<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NAlert, NButton, NCard, NForm, NFormItem, NInput } from 'naive-ui'
import { ApiError, auth } from '../api/client'
import { setAuthed } from '../router'

const username = ref(''), password = ref(''), loading = ref(false), error = ref('')
const route = useRoute(), router = useRouter()

async function submit() {
  if (!username.value || !password.value) { error.value = '请输入用户名和密码'; return }
  loading.value = true; error.value = ''
  try {
    await auth.login(username.value, password.value)
    password.value = ''
    setAuthed(true)
    const r = typeof route.query.redirect === 'string' && route.query.redirect.startsWith('/') ? route.query.redirect : '/'
    router.replace(r)
  } catch (e) {
    error.value = e instanceof ApiError && e.status === 429 ? '失败次数过多，请 15 分钟后再试' : '用户名或密码错误'
  } finally { loading.value = false }
}
</script>
<style scoped>
.wrap { min-height: 100%; display: flex; align-items: center; justify-content: center; background: #f4f6fa; }
.hint { margin: 0 0 12px; color: #667; font-size: 13px; }
</style>
