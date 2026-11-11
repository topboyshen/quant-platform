# 容器镜像与磁盘治理

Quant 集成 API 使用 `Dockerfile.integration`，只包含 Flask、Gunicorn 和三个只读 API 源文件。它不再继承 Dashboard 的 pandas、numpy、pyarrow、akshare 依赖。

`radar-image-prune.timer` 每周日运行一次：

- 每个 Radar 镜像仓库保留最新 2 个有标签版本；
- 被运行中或已停止容器引用的镜像永不删除；
- 清理超过 7 天的悬空镜像层与构建缓存；
- 脚本默认仅预览，只有 `--apply` 才会修改镜像。

人工预览：

```bash
scripts/prune_radar_images.sh --retain 2
```

执行：

```bash
sudo scripts/prune_radar_images.sh --apply --retain 2
```
