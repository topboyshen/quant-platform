import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import AsyncState from './AsyncState.vue'

const slots = { default: '<p class="content">内容</p>' }

describe('AsyncState', () => {
  it('shows the error and hides the slot', () => {
    const wrapper = mount(AsyncState, { props: { error: '请求失败' }, slots })
    expect(wrapper.text()).toContain('请求失败')
    expect(wrapper.find('.content').exists()).toBe(false)
  })

  it('shows the empty text and hides the slot', () => {
    const wrapper = mount(AsyncState, { props: { empty: true, emptyText: '暂无记录' }, slots })
    expect(wrapper.text()).toContain('暂无记录')
    expect(wrapper.find('.content').exists()).toBe(false)
  })

  it('renders the slot when there is data', () => {
    const wrapper = mount(AsyncState, { slots })
    expect(wrapper.find('.content').text()).toBe('内容')
  })
})
