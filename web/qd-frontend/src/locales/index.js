import Vue from 'vue'
import VueI18n from 'vue-i18n'
import storage from 'store'
import enUS from './lang/en-US'
import zhCN from './lang/zh-CN'
import copilotOverrides from './copilot-overrides'
import profileSecurityMessages from './lang/profile-security'
import brokerAccountWorkspaceMessages from './lang/broker-account-workspace'
import strategyV2Messages from './lang/strategy-v2'
import strategyLiveRiskMessages from './lang/strategy-live-risk'
import robotBuilderMessages from './lang/robot-builder-overrides'
import strategyTradeRecordMessages from './lang/strategy-trade-records'

Vue.use(VueI18n)

export const defaultLang = 'zh-CN'

const messages = {
  'en-US': {
    ...enUS,
    ...(copilotOverrides['en-US'] || {}),
    ...(profileSecurityMessages['en-US'] || {}),
    ...(brokerAccountWorkspaceMessages['en-US'] || {}),
    ...(strategyV2Messages['en-US'] || {}),
    ...(strategyLiveRiskMessages['en-US'] || {}),
    ...(robotBuilderMessages['en-US'] || {}),
    ...(strategyTradeRecordMessages['en-US'] || {})
  },
  [defaultLang]: {
    ...zhCN,
    ...(copilotOverrides[defaultLang] || {}),
    ...(profileSecurityMessages[defaultLang] || {}),
    ...(brokerAccountWorkspaceMessages[defaultLang] || {}),
    ...(strategyV2Messages[defaultLang] || {}),
    ...(strategyLiveRiskMessages[defaultLang] || {}),
    ...(robotBuilderMessages[defaultLang] || {}),
    ...(strategyTradeRecordMessages[defaultLang] || {})
  }
}

const i18n = new VueI18n({
  silentTranslationWarn: true,
  silentFallbackWarn: true,
  locale: defaultLang,
  fallbackLocale: 'en-US',
  messages
})

const loadedLanguages = [defaultLang, 'en-US']

function setI18nLanguage (lang) {
  i18n.locale = lang
  const html = document.documentElement
  const isRtl = /^ar/i.test(lang)
  if (html) {
    html.setAttribute('lang', lang)
    html.setAttribute('dir', isRtl ? 'rtl' : 'ltr')
  }
  if (document.body) {
    document.body.setAttribute('dir', isRtl ? 'rtl' : 'ltr')
    document.body.classList.toggle('rtl', isRtl)
  }
  return lang
}

function mergeLocaleOverrides (lang) {
  const overrides = {
    ...(copilotOverrides[lang] || {}),
    ...(profileSecurityMessages[lang] || {}),
    ...(brokerAccountWorkspaceMessages[lang] || {}),
    ...(strategyV2Messages[lang] || {}),
    ...(strategyLiveRiskMessages[lang] || {}),
    ...(robotBuilderMessages[lang] || {}),
    ...(strategyTradeRecordMessages[lang] || {})
  }
  i18n.setLocaleMessage(lang, {
    ...(i18n.getLocaleMessage(lang) || {}),
    ...overrides
  })
}

export async function loadLanguageAsync (lang = defaultLang) {
  storage.set('lang', lang)
  if (i18n.locale === lang) {
    mergeLocaleOverrides(lang)
    return setI18nLanguage(lang)
  }

  if (!loadedLanguages.includes(lang)) {
    return setI18nLanguage(defaultLang)
  }

  mergeLocaleOverrides(lang)
  return setI18nLanguage(lang)
}

export function i18nRender (key) {
  return i18n.t(`${key}`)
}

export default i18n
