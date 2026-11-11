<template><div ref="el" :style="{ height, width: '100%' }" /></template>
<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart, GaugeChart, HeatmapChart, LineChart, PieChart } from 'echarts/charts'
import { CalendarComponent, DataZoomComponent, GridComponent, LegendComponent, TitleComponent, TooltipComponent, VisualMapComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([BarChart, PieChart, GaugeChart, LineChart, HeatmapChart, GridComponent, LegendComponent, TitleComponent, TooltipComponent, CalendarComponent, VisualMapComponent, DataZoomComponent, CanvasRenderer])
const props = withDefaults(defineProps<{ option: Record<string, unknown>; height?: string }>(), { height: '280px' })
const el = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null
const resize = () => chart?.resize()
onMounted(() => { chart = echarts.init(el.value!); chart.setOption(props.option); window.addEventListener('resize', resize) })
watch(() => props.option, (o) => chart?.setOption(o, true), { deep: true })
onBeforeUnmount(() => { window.removeEventListener('resize', resize); chart?.dispose() })
</script>
