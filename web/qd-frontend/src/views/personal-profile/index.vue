<template>
  <div class="personal-profile" :class="{ dark: isDark }">
    <div class="page-head">
      <div>
        <h2><a-icon type="user" /> 个人设置</h2>
        <p>仅管理当前管理员账户；个人模式不包含会员、积分、邀请和多用户管理。</p>
      </div>
      <a-tag color="blue">PERSONAL ADMIN</a-tag>
    </div>

    <a-row :gutter="18">
      <a-col :xs="24" :lg="10">
        <a-card title="账户信息" :bordered="false" class="profile-card">
          <a-spin :spinning="loading">
            <div class="identity">
              <a-avatar :size="72" :src="profile.avatar || '/avatar2.jpg'" />
              <div>
                <strong>{{ profile.nickname || profile.username || '--' }}</strong>
                <span>{{ profile.username || '--' }}</span>
                <a-tag color="green">{{ roleLabel }}</a-tag>
              </div>
            </div>
            <a-form-model :model="profileForm" layout="vertical" class="form-block">
              <a-form-model-item label="昵称">
                <a-input v-model="profileForm.nickname" maxlength="60" />
              </a-form-model-item>
              <a-form-model-item label="时区">
                <a-select v-model="profileForm.timezone" show-search allow-clear option-filter-prop="children">
                  <a-select-option value="">跟随浏览器</a-select-option>
                  <a-select-option v-for="timezone in timezones" :key="timezone" :value="timezone">{{ timezone }}</a-select-option>
                </a-select>
              </a-form-model-item>
              <a-button type="primary" icon="save" :loading="saving" @click="saveProfile">保存账户信息</a-button>
            </a-form-model>
          </a-spin>
        </a-card>
      </a-col>

      <a-col :xs="24" :lg="14">
        <a-card title="修改密码" :bordered="false" class="profile-card">
          <a-alert type="info" show-icon message="公网部署请使用至少 12 位的独立强密码；修改后旧会话将失效。" />
          <a-form-model ref="passwordForm" :model="passwordForm" :rules="passwordRules" layout="vertical" class="form-block">
            <a-form-model-item label="当前密码" prop="old_password">
              <a-input-password v-model="passwordForm.old_password" autocomplete="current-password" />
            </a-form-model-item>
            <a-form-model-item label="新密码" prop="new_password">
              <a-input-password v-model="passwordForm.new_password" autocomplete="new-password" />
            </a-form-model-item>
            <a-form-model-item label="确认新密码" prop="confirm_password">
              <a-input-password v-model="passwordForm.confirm_password" autocomplete="new-password" />
            </a-form-model-item>
            <a-button type="primary" icon="key" :loading="changing" @click="changeAccountPassword">修改密码</a-button>
          </a-form-model>
        </a-card>
      </a-col>
    </a-row>

    <a-card title="个人模式边界" :bordered="false" class="boundary-card">
      <a-row :gutter="12">
        <a-col v-for="item in boundaries" :key="item.title" :xs="24" :sm="12" :xl="6">
          <div class="boundary-item">
            <a-icon :type="item.icon" />
            <div><strong>{{ item.title }}</strong><span>{{ item.desc }}</span></div>
          </div>
        </a-col>
      </a-row>
    </a-card>
  </div>
</template>

<script>
import { mapState } from 'vuex'
import { getProfile, updateProfile, changePassword } from '@/api/user'

export default {
  name: 'PersonalProfile',
  data () {
    const confirmPassword = (rule, value, callback) => {
      if (value !== this.passwordForm.new_password) callback(new Error('两次输入的新密码不一致'))
      else callback()
    }
    return {
      loading: false,
      saving: false,
      changing: false,
      profile: {},
      profileForm: { nickname: '', timezone: '' },
      passwordForm: { old_password: '', new_password: '', confirm_password: '' },
      passwordRules: {
        old_password: [{ required: true, message: '请输入当前密码', trigger: 'blur' }],
        new_password: [
          { required: true, message: '请输入新密码', trigger: 'blur' },
          { min: 12, message: '新密码至少 12 位', trigger: 'blur' }
        ],
        confirm_password: [
          { required: true, message: '请再次输入新密码', trigger: 'blur' },
          { validator: confirmPassword, trigger: 'blur' }
        ]
      },
      timezones: ['Asia/Shanghai', 'Asia/Hong_Kong', 'Asia/Tokyo', 'Europe/London', 'America/New_York'],
      boundaries: [
        { icon: 'safety-certificate', title: '单管理员', desc: '关闭注册、OAuth 与用户管理' },
        { icon: 'wallet', title: '无收费模块', desc: '关闭会员、积分与 USDT 支付' },
        { icon: 'shop', title: '无社区市场', desc: '指标仅用于自己的研究工作区' },
        { icon: 'stop', title: '无自动实盘', desc: '关闭券商凭据、快捷交易和执行线程' }
      ]
    }
  },
  computed: {
    ...mapState({ navTheme: state => state.app.theme }),
    isDark () { return ['dark', 'realdark'].includes(this.navTheme) },
    roleLabel () {
      const role = this.profile.role
      return typeof role === 'object' ? (role.id || 'admin') : (role || 'admin')
    }
  },
  created () { this.loadProfile() },
  methods: {
    async loadProfile () {
      this.loading = true
      try {
        const response = await getProfile()
        if (!response || response.code !== 1 || !response.data) throw new Error((response && response.msg) || '账户信息加载失败')
        this.profile = response.data
        this.profileForm.nickname = response.data.nickname || ''
        this.profileForm.timezone = response.data.timezone || ''
      } catch (error) {
        this.$message.error(error.backendMessage || error.message || '账户信息加载失败')
      } finally {
        this.loading = false
      }
    },
    async saveProfile () {
      this.saving = true
      try {
        const response = await updateProfile(this.profileForm)
        if (!response || response.code !== 1) throw new Error((response && response.msg) || '保存失败')
        this.$message.success('账户信息已保存')
        await this.loadProfile()
        await this.$store.dispatch('FetchUserInfo')
      } catch (error) {
        this.$message.error(error.backendMessage || error.message || '保存失败')
      } finally {
        this.saving = false
      }
    },
    changeAccountPassword () {
      this.$refs.passwordForm.validate(async valid => {
        if (!valid) return
        this.changing = true
        try {
          const response = await changePassword({
            old_password: this.passwordForm.old_password,
            new_password: this.passwordForm.new_password
          })
          if (!response || response.code !== 1) throw new Error((response && response.msg) || '密码修改失败')
          this.$message.success('密码已修改，请使用新密码重新登录')
          this.passwordForm = { old_password: '', new_password: '', confirm_password: '' }
          await this.$store.dispatch('Logout')
          this.$router.replace({ name: 'login' })
        } catch (error) {
          this.$message.error(error.backendMessage || error.message || '密码修改失败')
        } finally {
          this.changing = false
        }
      })
    }
  }
}
</script>

<style lang="less" scoped>
.personal-profile { padding: 8px; }
.page-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 18px; }
.page-head h2 { margin: 0 0 6px; font-size: 24px; }
.page-head p { margin: 0; color: #7b8494; }
.profile-card { min-height: 480px; margin-bottom: 18px; border-radius: 12px; box-shadow: 0 4px 16px rgba(15,23,42,.06); }
.identity { display: flex; align-items: center; gap: 16px; padding: 10px 0 22px; border-bottom: 1px solid #edf0f4; }
.identity > div { display: flex; flex-direction: column; align-items: flex-start; gap: 5px; }
.identity strong { font-size: 18px; }
.identity span { color: #8a94a6; }
.form-block { margin-top: 22px; }
.boundary-card { border-radius: 12px; box-shadow: 0 4px 16px rgba(15,23,42,.05); }
.boundary-item { display: flex; gap: 12px; min-height: 82px; padding: 14px; border: 1px solid #e8ecf1; border-radius: 10px; }
.boundary-item > .anticon { color: var(--primary-color,#1890ff); font-size: 22px; }
.boundary-item div { display: flex; flex-direction: column; gap: 5px; }
.boundary-item span { color: #8a94a6; font-size: 12px; }
.dark .profile-card, .dark .boundary-card { background: #202329; }
.dark .identity, .dark .boundary-item { border-color: #343840; }
@media (max-width: 600px) { .page-head { flex-direction: column; gap: 10px; } }
</style>
