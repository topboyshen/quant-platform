# Radar Swing EOD 核心调度

趋势波段核心入口（`radar_swing_eod.py`）每个交易日收盘后 15:10 机械运行一次：

- 只读取 `config.swing.json` 作为 ETF 趋势波段主配置。
- 只在收盘后确认周线 MACD 金叉、放量突破 MA60、情绪 5%/95% 历史极值。
- 盘中不看盘、不追单；节假日由脚本内部 `ScanScheduler.is_trading_day()` 过滤。

## 生产路径约定

systemd 单元以 `/opt/trading_radar` 为生产部署目录，虚拟环境为 `/opt/trading_radar/.venv`：

```bash
sudo mkdir -p /opt/trading_radar
sudo rsync -a --delete /path/to/trading-radar/ /opt/trading_radar/
cd /opt/trading_radar
/opt/trading_radar/.venv/bin/python -m pip install -e ".[monitor,dashboard]"
```

## 依赖要求

Swing EOD 需要 `akshare` 采集全市场成交额、两融余额和涨跌停家数。若运行时报 `akshare 不可用`，请在 systemd 使用的同一个虚拟环境内安装 Dashboard/Swing 依赖：

```bash
cd /opt/trading_radar
/opt/trading_radar/.venv/bin/python -m pip install -e ".[monitor,dashboard]"
```

## 方式一：systemd timer（推荐）

```bash
sudo cp deploy/systemd/radar-swing-eod.service /etc/systemd/system/
sudo cp deploy/systemd/radar-swing-eod.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now radar-swing-eod.timer

# 查看下一次触发时间
systemctl list-timers radar-swing-eod.timer
# 手动跑一次（验证）
sudo systemctl start radar-swing-eod.service
journalctl -u radar-swing-eod.service -f

# 不推送 Telegram 的核心链路试运行
cd /opt/trading_radar
/opt/trading_radar/.venv/bin/python /opt/trading_radar/radar_swing_eod.py --config /opt/trading_radar/config.swing.json --dry-run --force
```

`OnCalendar=Mon..Fri 15:10` 使用系统时区。若服务器不是 `Asia/Shanghai`，请
`sudo timedatectl set-timezone Asia/Shanghai`，或把时间换算到 UTC。
节假日不必在 timer 处理：`radar_swing_eod.py` 内部用 `ScanScheduler.is_trading_day()`
过滤，非交易日直接跳过（除非 `--force`）。

## 方式二：cron

```cron
# 交易日近似：周一至周五 15:10（节假日由脚本内部跳过）
10 15 * * 1-5 cd /opt/trading_radar && /opt/trading_radar/.venv/bin/python radar_swing_eod.py --config config.swing.json >> logs/swing_cron.log 2>&1
```

## 试运行（不推送 Telegram）

```bash
cd /opt/trading_radar
/opt/trading_radar/.venv/bin/python /opt/trading_radar/radar_swing_eod.py --config /opt/trading_radar/config.swing.json --dry-run --force
```
