# nnG8 测试入口约定（每组件一条命令）

版本化副本：`/home/ubuntu/signal-bus/docs/RUN_TESTS.md`。本文件与该副本在发布时按字节核对；修改测试入口时同批更新。下列数量是 2026-10-16 已留存结果，后续以实际输出为准。

| 组件 | 命令（在组件目录内执行） | 说明 |
| --- | --- | --- |
| research-data | `./.venv/bin/python run_offline_tests.py` | 含审计钩子与隔离环境；用系统 Python 跑会因缺 numpy 报 22 个 import error |
| signal-bus | `DASHBOARD_TEST_ROOT=/home/ubuntu/ops/dashboard DELIVERY_CONTRACT_TEST_MODULE=/home/ubuntu/signal-bus/scripts/delivery_evidence.py /usr/bin/python3 -B -m unittest discover -s tests` | 现役目录曾记录 277 项；本轮固定 Git 副本为 265 项（3 跳过），另有 3 个被忽略的 legacy 测试文件未进入提交或本轮隔离套件，不混称 277 项复验通过 |
| AStock_AI_System | `npm test` | node --test，全绿（fail 0） |
| qlib 增量 | `cd /home/ubuntu/qlib/scripts/qlib_dolt_incremental && /usr/bin/python3 -B -m unittest discover` | 372 项 |
| ops/dashboard exporter | `cd /home/ubuntu/ops/dashboard/exporter/tests && DELIVERY_CONTRACT_TEST_MODULE=/home/ubuntu/signal-bus/scripts/delivery_evidence.py /usr/bin/python3 -B -m unittest discover -s .` | 117 项（1 跳过） |

记录结果时必须同时记录：命令、提交/文件哈希、完整输出。不同入口的数字不可直接比较。

隔离布局：将上述 `DASHBOARD_TEST_ROOT` 和 `DELIVERY_CONTRACT_TEST_MODULE` 指向本次固定的隔离代码副本，禁止从证据输入目录动态寻找模块。另设 `ASTOCK_TEMPLATE_ROOT` 指向含 `systemd/` 的固定 AStock 模板副本；缺模板必须失败，不跳过或改断言。research-data 临时政策和 core 数据库路径必须位于 `run_offline_tests.py` 本轮 scratch 内；保留离线审计守卫和真实历史检查。
