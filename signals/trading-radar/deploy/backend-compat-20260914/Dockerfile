FROM sha256:0fb958dd9d7ab220ddf961600c838140f985e11f0de35976ac1dabb255c22e9c
LABEL org.opencontainers.image.version="personal-2026.09.14-compat.1" \
      quantdinger.compat.upstream="ff195f437b9e692cb7cc516e9f4cdb2138fe8cd0" \
      quantdinger.compat.base="sha256:0fb958dd9d7ab220ddf961600c838140f985e11f0de35976ac1dabb255c22e9c"
COPY --chown=0:0 app/services/backtest.py /app/app/services/backtest.py
COPY --chown=0:0 app/data_sources/tencent.py /app/app/data_sources/tencent.py
COPY --chown=0:0 app/data_sources/cn_stock.py /app/app/data_sources/cn_stock.py
