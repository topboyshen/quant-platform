#!/usr/bin/env python3
"""启动 ML 推理服务：python serve_ml.py（默认 0.0.0.0:8090）。"""

from radar.ml_serve.app import main

if __name__ == "__main__":
    main()
