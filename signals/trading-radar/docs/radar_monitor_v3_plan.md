# Radar Monitor V3 升级方案

## 1. 现状诊断总结

### 1.1 架构现状
- **入口**: `radar_monitor.py` (214行) → `radar_monitor_v2.py` (213行)
- **引擎**: `monitor_engine.py` (544行) → `monitor_engine_v2.py` (479行) + 6个Mixin
- **数据层**: `market_data.py` (515行) → `market_data_v2.py` (618行) + `market_data_concurrent_v2.py` (247行)
- **冷却**: `cooldown.py` → `cooldown_v2.py` (292行，内存缓存+SQLite)
- **通知**: `telegram.py` (168行)
- **策略**: `base.py` (81行) + `ml_strategy.py` + `rl_strategy_v2.py` (400行)
- **状态**: `battlefield_state.py` (563行)

### 1.2 已实现的优化（V2）
✅ 数据源降级链（东财→腾讯→akshare）
✅ 断路器保护（5次失败熔断，30秒恢复）
✅ 并发行情获取（智能策略选择：≤15单股并行，>100批量缓存）
✅ 批量缓存行级索引（O(1)查找）
✅ 冷却管理器双层架构（内存L1 + SQLite L2）
✅ Battlefield状态持久化（SQLite WAL模式）
✅ 性能指标采集（fetch/process时间分离）
✅ 慢扫描告警（>5秒）
✅ 优雅退出（try/finally保证状态保存）
✅ systemd环境变量修复（/proc/self/environ直接读取）
✅ 健康检查服务器（/health, /metrics, /ready, /live）

### 1.3 现存问题
🔴 **服务仍在运行旧版**: `systemctl`显示运行的是`radar_monitor.py`，不是V2
🔴 **502错误频发**: 东财批量接口持续502，熔断器频繁触发
🔴 **扫描耗时波动**: 7-14秒，目标60秒间隔，实际占用10-23%时间
🔴 **内存占用高**: 峰值118.8M，对于7只股票偏高
🔴 **策略层耦合**: ML/RL策略直接加载模型，启动慢
🔴 **缺乏指标聚合**: 没有Prometheus/Grafana集成
🔴 **告警噪音**: 数据源熔断告警过于频繁
🔴 **无自适应间隔**: 非交易时段仍固定300秒休眠

## 2. V3 升级目标

### 2.1 性能目标
- 扫描耗时: 7-14s → <3s (70%提升)
- 内存峰值: 118M → <80M
- 启动时间: <5s
- CPU占用: 降低50%

### 2.2 可靠性目标
- 数据源可用率: >99.5%
- 告警准确率: >95%
- 零状态丢失

### 2.3 可观测性目标
- Prometheus指标自动暴露
- 结构化日志（JSON）
- 性能面板

## 3. V3 架构设计

### 3.1 核心改进

#### A. 数据源层重构
```
DataSourceManager (新)
├── PrimarySource: 东财批量 (缓存3秒TTL)
├── SecondarySource: 腾讯单股 (独立session)
├── TertiarySource: akshare (本地库)
└── FallbackSource: 缓存数据 (过期容忍)
```

改进点:
1. **连接池复用**: 所有源共享Session池，减少TCP开销
2. **智能预取**: 开盘前5分钟预热缓存
3. **502快速失败**: 检测到502立即切换，不浪费重试
4. **数据新鲜度标记**: 每个数据点带timestamp，策略层可判断

#### B. 策略引擎异步化
```
StrategyEngine (新)
├── PriceStrategy: 同步执行（简单判断）
├── MLStrategy: 异步执行（线程池，不阻塞主循环）
└── RLStrategy: 异步执行（延迟评估）
```

改进点:
1. **策略并行**: ML/RL不再阻塞价格触发
2. **模型懒加载**: 首次需要时才加载模型
3. **预测缓存**: ML结果缓存30秒
4. **批量推理**: 多只股票合并一次模型调用

#### C. 告警系统升级
```
AlertManager (新)
├── Deduplicator: 去重（内容hash）
├── Throttler: 限流（每分钟最多N条）
├── Batcher: 合并（5秒窗口）
└── Router: 路由（价格/ML/RL/系统分级）
```

改进点:
1. **告警分级**: Critical/Warning/Info，不同通道
2. **静默时段**: 非交易时段只发Critical
3. **聚合报告**: 每小时汇总一次
4. **反压机制**: Telegram API限流时自动降级

#### D. 扫描调度优化
```
ScanScheduler (新)
├── AdaptiveInterval: 根据市场活跃度调整
├── PreMarketWarmup: 开盘前预热
├── PostMarketCleanup: 收盘后整理
└── HolidayDetector: 节假日自动跳过
```

改进点:
1. **自适应间隔**: 交易活跃期30秒，平淡期120秒
2. **预热机制**: 09:25开始预取数据
3. **节假日表**: 内置A股休市日历

#### E. 可观测性增强
```
Observability (新)
├── MetricsCollector: Prometheus格式
├── PerformanceProfiler: 自动火焰图
├── HealthChecker: 深度健康检查
└── AlertOnAnomaly: 自我监控告警
```

## 4. 实施计划

### Phase 1: 数据源层重构 (2h)
- [ ] 创建 `radar/data/datasource_manager.py`
- [ ] 实现连接池复用
- [ ] 502快速检测+切换
- [ ] 数据新鲜度标记

### Phase 2: 策略引擎异步化 (2h)
- [ ] 创建 `radar/core/strategy_engine.py`
- [ ] 实现策略线程池
- [ ] ML模型懒加载
- [ ] 预测结果缓存

### Phase 3: 告警系统升级 (1.5h)
- [ ] 创建 `radar/alert/alert_manager.py`
- [ ] 实现去重+限流+分级
- [ ] Telegram反压处理

### Phase 4: 扫描调度优化 (1h)
- [ ] 创建 `radar/core/scan_scheduler.py`
- [ ] 自适应间隔算法
- [ ] 节假日检测

### Phase 5: 可观测性 (1h)
- [ ] Prometheus指标暴露
- [ ] 性能面板端点

### Phase 6: 集成测试+部署 (1.5h)
- [ ] 创建 `radar_monitor_v3.py`
- [ ] 压力测试
- [ ] 平滑切换

总计: ~9h

## 5. 关键设计决策

### 5.1 为什么不用asyncio?
- 现有代码大量同步IO（requests, akshare, sqlite）
- 改为asyncio需要重写所有依赖
- ThreadPoolExecutor足够，且兼容性好

### 5.2 数据源优先级
1. 东财批量（全市场5000只，一次请求）
2. 腾讯单股（备用，独立session）
3. akshare（本地库，无网络依赖）
4. 缓存数据（过期容忍，保证连续性）

### 5.3 内存优化策略
- 批量数据获取后只保留目标股票
- DataFrame转dict后立即释放
- 模型推理后卸载中间结果
- gc.collect()在扫描间隙触发

## 6. 风险与回滚

### 6.1 风险
- 新数据源管理器引入bug导致数据缺失
- 异步策略导致告警延迟
- 自适应间隔错过关键价格变动

### 6.2 缓解
- 保留V2代码，并行运行对比
- 增加shadow模式（只记录不发告警）
- 配置开关可关闭新特性

### 6.3 回滚
```bash
# 紧急回滚到V2
sudo systemctl stop stock-radar
sudo ln -sf radar_monitor_v2.py radar_monitor.py
sudo systemctl start stock-radar
```
