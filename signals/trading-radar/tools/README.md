# 🚀 Radar 工具集

这里放置了所有与 trading_radar 系统配套的工具脚本。

---

## 📋 系统管理工具

### radarctl - 系统控制台
```bash
radarctl status              # 查看系统状态
radarctl restart             # 重启服务
radarctl logs [N]            # 查看最新N行日志
radarctl test-alert          # 发送测试告警
radarctl backup              # 备份配置和数据
```

### 系统清理
```bash
./tools/system_cleanup.sh    # 清理旧日志、缓存、备份
```

---

## 📊 金融数据工具

### mx - 妙想数据查询
```bash
mx "茅台最新价"
mx "东方财富资金流向"
mx "比亚迪近一年净利润"
```

### dfcf - 东方财富爬虫
```bash
dfcf stock 300059           # 获取股票详情
dfcf stock 300059 --no-camoufox  # 使用标准浏览器
dfcf sector                 # 获取板块排名
dfcf news 300059            # 获取股票新闻
```

### 自动数据收集
```bash
# 每日 15:35 自动运行，收集所有监控股票的资金流向
./tools/collect_daily.sh
```

### 每日报告
```bash
# 每日 16:00 自动运行，发送 Telegram 报告
./tools/daily_report.py
```

---

## 🌐 浏览器自动化工具

### webshot - 网页截图
```bash
# 基本截图
webshot "https://www.example.com"
webshot "https://www.example.com" --output /path/to/save.png

# 使用 Camoufox 反检测浏览器
webshot "https://www.example.com" --camoufox

# 等待更长时间（动态加载页面）
webshot "https://www.example.com" --wait 10

# 监控模式（对比变化）
webshot "https://www.example.com" --monitor
```

### dfcf - 东方财富爬虫
支持 Camoufox 反检测模式

```bash
# 获取股票详情
python tools/dfcf_spider.py stock 300059

# 使用 Camoufox
python tools/dfcf_spider.py stock 300059 --no-camoufox
```

### monitor - 自动监控
```bash
# 手动运行监控检查
monitor

# 添加到 crontab 每2小时检查一次
0 */2 * * * /home/ubuntu/trading_radar/venv/bin/python /home/ubuntu/trading_radar/tools/auto_monitor.py
```

---

## 📝 配置文件

### 监控配置 (auto_monitor.py)
编辑 `tools/auto_monitor.py` 中的 `MONITOR_CONFIG`:

```python
MONITOR_CONFIG = {
    'urls': [
        'https://quote.eastmoney.com/concept/sh300059.html',
        'https://www.example.com/news',
    ],
    'keywords': [
        '重大利好',
        '重大利空',
    ]
}
```

---

## ⏰ 定时任务

当前配置的定时任务（`crontab -l` 查看）：

```
0 3 * * *      # 每天凌晨03:00 清理30天前的RL特征缓存
35 15 * * 1-5  # 工作日15:35 收集资金流向数据
0 16 * * 1-5   # 工作日16:00 发送每日市场报告
```

---

## 📁 目录结构

```
tools/
├── radarctl.py              # 系统管理控制台
├── mx_query.py              # 妙想数据查询
├── dfcf_spider.py           # 东方财富爬虫
├── web_screenshot.py        # 网页截图
├── auto_monitor.py          # 自动监控
├── daily_report.py          # 每日报告生成
├── daily_data_collector.py  # 每日数据收集
├── collect_daily.sh         # 每日收集脚本
├── system_cleanup.sh        # 系统清理
└── README.md                # 本文件
```

---

## ⚠️ 注意事项

1. **Camoufox vs Playwright**
   - Camoufox: 反检测能力更强，适合爬取有反爬机制的网站
   - Playwright: 速度更快，适合一般网页

2. **资源占用**
   - 浏览器自动化需要较多内存（~200-400MB）
   - 建议在非高峰时段运行爬虫

3. **法律合规**
   - 请遵守网站 robots.txt 规则
   - 不要过频请求，避免对目标网站造成负担
