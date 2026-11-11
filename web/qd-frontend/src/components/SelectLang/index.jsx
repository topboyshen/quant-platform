import './index.less'

import { Icon, Menu, Dropdown } from 'ant-design-vue'
import { i18nRender } from '@/locales'
import i18nMixin from '@/store/i18n-mixin'

const locales = [
  'en-US',
  'zh-CN'
]

const languageLabels = {
  'zh-CN': '\u7b80\u4f53\u4e2d\u6587',
  'en-US': 'English'
}

const languageIcons = {
  'zh-CN': 'CN',
  'en-US': 'EN'
}

const languageShortLabels = {
  'zh-CN': '\u4e2d\u6587',
  'en-US': 'EN'
}

const SelectLang = {
  props: {
    prefixCls: {
      type: String,
      default: 'ant-pro-drop-down'
    }
  },
  name: 'SelectLang',
  mixins: [i18nMixin],
  render () {
    const { prefixCls } = this
    const changeLang = ({ key }) => {
      this.setLang(key)
    }
    const handleKeydown = event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault()
        event.currentTarget.click()
      }
    }
    const langMenu = (
      <Menu class={['menu', 'ant-pro-header-menu']} selectedKeys={[this.currentLang]} onClick={changeLang}>
        {locales.map(locale => (
          <Menu.Item key={locale}>
            <span class="language-code" aria-label={languageLabels[locale]}>
              {languageIcons[locale]}
            </span>
            {languageLabels[locale]}
          </Menu.Item>
        ))}
      </Menu>
    )
    const currentLabel = languageShortLabels[this.currentLang] || 'Lang'
    const translated = i18nRender('navBar.lang')
    const title = `${translated === 'navBar.lang' ? (this.currentLang === 'zh-CN' ? '语言' : 'Language') : translated} · ${languageLabels[this.currentLang] || currentLabel}`
    return (
      <Dropdown overlay={langMenu} placement="bottomRight" trigger={['click']}>
        <span class={[prefixCls, 'language-action']} title={title} aria-label={title} role="button" tabIndex="0" onKeydown={handleKeydown}>
          <Icon type="global" class="language-action-icon" />
          <span class="language-action-label">{currentLabel}</span>
        </span>
      </Dropdown>
    )
  }
}

export default SelectLang
