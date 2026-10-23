// D8 / R8：ECharts 主题抽离为独立模块，颜色从「编辑出版物风」浅色 token 派生，
// 不再在 App.vue 里硬编码深色值。后端契约不变，仅影响前端图表外观。

export const editorialTheme = {
  color: ['#2f4b7c', '#2f6b4f', '#9a6a00', '#a83232', '#6b5b95', '#3a7ca5'],
  backgroundColor: 'transparent',
  textStyle: { color: '#3a3a3a', fontFamily: '"Noto Serif SC", "Songti SC", "SimSun", serif' },
  title: {
    textStyle: { color: '#2b2b2b', fontSize: 14, fontWeight: 600 },
    subtextStyle: { color: '#8a8a8a', fontSize: 11 },
  },
  tooltip: {
    trigger: 'axis',
    backgroundColor: '#ffffff',
    borderColor: '#e3ddd2',
    borderWidth: 1,
    textStyle: { color: '#2b2b2b', fontSize: 12 },
  },
  legend: { textStyle: { color: '#6b6b6b' } },
  grid: { left: 52, right: 24, bottom: 44, top: 56, containLabel: true },
  categoryAxis: {
    axisLine: { lineStyle: { color: '#cfc7b8' } },
    axisTick: { show: false },
    axisLabel: { color: '#6b6b6b', fontSize: 11 },
    splitLine: { show: false, lineStyle: { color: '#ece6da' } },
  },
  valueAxis: {
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: '#6b6b6b', fontSize: 11 },
    splitLine: { lineStyle: { color: '#ece6da', type: 'dashed' } },
  },
  series: {
    bar: { itemStyle: { borderRadius: [3, 3, 0, 0] } },
    line: { smooth: true, symbol: 'circle', symbolSize: 6 },
    pie: {
      label: { color: '#3a3a3a', fontSize: 12 },
      itemStyle: { borderColor: '#ffffff', borderWidth: 2 },
    },
  },
}
