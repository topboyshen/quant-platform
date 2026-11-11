<template><div ref="el" :style="{ height, width: '100%' }" /></template>
<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart, GaugeChart, PieChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TitleComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([BarChart, PieChart, GaugeChart, GridComponent, LegendComponent, TitleComponent, TooltipComponent, CanvasRenderer])
const props = withDefaults(defineProps<{ option: Record<string, unknown>; height?: string }>(), { height: '280px' })
const el = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null
const resize = () => chart?.resize()
onMounted(() => { chart = echarts.init(el.value!); chart.setOption(props.option); window.addEventListener('resize', resize) })
watch(() => props.option, (o) => chart?.setOption(o, true), { deep: true })
onBeforeUnmount(() => { window.removeEventListener('resize', resize); chart?.dispose() })
</script>
