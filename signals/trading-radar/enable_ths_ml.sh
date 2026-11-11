#!/bin/bash
# 同花顺ML恢复脚本 - 除权后重新启用ML预测
# 运行时间: 明天开盘后（建议9:30后）

cd /home/ubuntu/trading_radar
source venv/bin/activate

echo "$(date '+%Y-%m-%d %H:%M:%S') - 开始恢复同花顺ML预测..."

# 1. 修改配置启用ML
python3 -c "
import json
with open('config.json', 'r') as f:
    config = json.load(f)

for target in config['targets']:
    if target['code'] == '300033':
        target['ml_enabled'] = True
        print(f'已启用 {target[\"name\"]} 的ML预测')
        break

with open('config.json', 'w') as f:
    json.dump(config, f, ensure_ascii=False, indent=2)
"

# 2. 训练同花顺模型
echo "训练同花顺模型..."
if [ -z "$MX_APIKEY" ]; then
    echo "错误: MX_APIKEY 环境变量未设置"
    exit 1
fi
python3 -c "
import json
import logging
import sys
sys.path.insert(0, '.')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('ths_train')

from ml_predictor import MLPredictor
from mx_client import MXDataClient

with open('config.json', 'r') as f:
    config = json.load(f)

# 找到同花顺
target = None
for t in config['targets']:
    if t['code'] == '300033':
        target = t
        break

if not target:
    print('错误: 未找到同花顺配置')
    sys.exit(1)

logger.info(f'训练 {target[\"name\"]} ({target[\"code\"]})...')

# 获取数据
mx_client = MXDataClient()
df = mx_client.get_stock_history(target['code'], target['name'], days=150)

if df is None or len(df) < 50:
    logger.error(f'数据不足: {len(df) if df else 0}条')
    sys.exit(1)

logger.info(f'获取到 {len(df)} 条数据')

# 训练模型
predictor = MLPredictor(model_dir='models', logger=logger)
result = predictor.train(
    code=target['code'],
    direction=target['direction'],
    df=df,
    trigger_price=target['trigger_price'],
    prediction_window=5
)

if result.get('success'):
    logger.info(f'训练成功! 准确率: {result[\"accuracy\"]:.1%}')
else:
    logger.error(f'训练失败: {result.get(\"reason\")}')
    sys.exit(1)
"

if [ $? -eq 0 ]; then
    # 3. 重启服务
    echo "重启雷达服务..."
    sudo systemctl daemon-reload
    sudo systemctl restart stock-radar.service
    
    echo "$(date '+%Y-%m-%d %H:%M:%S') - 同花顺ML预测已恢复!"
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') - 训练失败，请检查日志"
    exit 1
fi
