#!/usr/bin/env python3
"""
强化学习代理模块
基于 Stable-Baselines3 PPO，集成自定义交易环境
支持：Early Stopping、多股票通用策略训练
"""

import logging
import os
import shutil
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import gymnasium as gym
import numpy as np
import pandas as pd
from stable_baselines3 import PPO
from stable_baselines3.common.callbacks import BaseCallback
from stable_baselines3.common.vec_env import DummyVecEnv, SubprocVecEnv

from rl_environment import StockTradingEnv


class TrainingLoggerCallback(BaseCallback):
    """训练日志回调"""

    def __init__(self, logger: logging.Logger, log_interval: int = 1000):
        super().__init__()
        self._custom_logger = logger
        self.log_interval = log_interval

    def _on_step(self) -> bool:
        if self.n_calls % self.log_interval == 0:
            mean_reward = np.mean(self.locals.get('rewards', [0]))
            self._custom_logger.info(
                f"RL Training step {self.n_calls} - mean_reward: {mean_reward:.4f}"
            )
        return True


class RLAgent:
    """
    强化学习交易代理

    使用 PPO 训练离散动作策略，支持：
    - 单股票训练 + Early Stopping（验证集）
    - 多股票通用策略（Market-Agnostic）训练
    - 保存/加载模型

    线程安全 / 部署建议：
    - predict() 和 load_model() 已用 threading.Lock() 串行化，可在单进程多线程下安全使用。
    - 但 stable-baselines3 的 PyTorch 推理本身受 GIL 限制，高并发场景推荐：
      1) 进程隔离：每个 gunicorn/uwsgi worker 独立持有一份 RLAgent（不共享）。
      2) 推理微服务：把 RL 推理拆成独立的 gRPC/HTTP 服务，内部单线程队列处理。
      3) 模型级隔离：一个 worker 只服务一个模型，彻底避免竞争。
    """

    def __init__(self, model_dir: str = "rl_models", logger: Optional[logging.Logger] = None):
        self.model_dir = model_dir
        self.logger = logger or logging.getLogger(__name__)
        self.models: Dict[str, PPO] = {}
        # 保护模型字典和 predict 的线程安全锁
        # 注：stable-baselines3 的 model.predict() 在 PyTorch 层面非线程安全，
        # 高并发部署时推荐进程隔离（gunicorn worker / 独立推理服务）
        self._model_lock = threading.Lock()

        os.makedirs(model_dir, exist_ok=True)

    def _get_model_path(self, code: str, direction: str) -> str:
        return os.path.join(self.model_dir, f"ppo_{code}_{direction}.zip")

    def _get_universal_model_path(self, direction: str) -> str:
        return os.path.join(self.model_dir, f"ppo_universal_{direction}.zip")

    def _backup_model_if_exists(self, model_path: str):
        """在覆盖旧模型前自动备份到 backups/ 目录"""
        if os.path.exists(model_path):
            backup_dir = os.path.join(self.model_dir, "backups")
            os.makedirs(backup_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_name = f"{timestamp}_{os.path.basename(model_path)}"
            backup_path = os.path.join(backup_dir, backup_name)
            shutil.copy2(model_path, backup_path)
            self.logger.info(f"📦 已备份旧模型: {backup_path}")

    def create_env(self, df: pd.DataFrame, trigger_price: float, direction: str,
                   initial_capital: float = 100000, position_pct: float = 0.2,
                   split: str = 'train', n_envs: int = 4,
                   cache_dir: str = "rl_feature_cache",
                   external_features=None, live_mode: bool = False,
                   **env_kwargs):
        """创建向量化环境，支持并行采样"""
        def _make_env(rank: int):
            def _init():
                env = StockTradingEnv(
                    df=df,
                    trigger_price=trigger_price,
                    direction=direction,
                    initial_capital=initial_capital,
                    position_pct=position_pct,
                    split=split,
                    cache_dir=cache_dir,
                    external_features=external_features,
                    live_mode=live_mode,
                    **env_kwargs
                )
                env.reset(seed=rank)
                return env
            return _init

        env_fns = [_make_env(i) for i in range(n_envs)]
        if n_envs == 1:
            return DummyVecEnv(env_fns)
        return SubprocVecEnv(env_fns)

    def create_universal_env(self, datasets: List[Tuple[pd.DataFrame, float, str, pd.DataFrame]],
                             initial_capital: float = 100000, position_pct: float = 0.2,
                             split: str = 'train', n_envs: int = 4,
                             cache_dir: str = "rl_feature_cache",
                             live_mode: bool = False):
        """
        创建通用策略并行环境：在多个股票数据集之间随机切换（每 episode 随机选一个）。

        datasets 格式: [(df, trigger_price, stock_code, external_df), ...]
        external_df 可选，为 None 时使用基础特征
        """
        class UniversalStockEnv(gym.Env):
            def __init__(self, datasets, initial_capital, position_pct, split, seed=0,
                         cache_dir="rl_feature_cache", live_mode=False):
                super().__init__()
                self.datasets = datasets
                self.initial_capital = initial_capital
                self.position_pct = position_pct
                self.split = split
                self._seed = seed
                self._cache_dir = cache_dir
                self._live_mode = live_mode

                # 取第一个数据集推断空间
                first_df, first_tp, first_direction, first_ext = datasets[0]
                first_env = StockTradingEnv(
                    df=first_df, trigger_price=first_tp,
                    direction=first_direction, initial_capital=initial_capital,
                    position_pct=position_pct, split=split,
                    cache_dir=cache_dir,
                    external_features=first_ext,
                    live_mode=live_mode
                )
                self.observation_space = first_env.observation_space
                self.action_space = first_env.action_space
                self.current_env = None

            def reset(self, seed=None, options=None):
                effective_seed = seed if seed is not None else self._seed
                dataset = self.datasets[np.random.randint(len(self.datasets))]
                df, tp, direction, external_df = dataset[0], dataset[1], dataset[2], dataset[3] if len(dataset) > 3 else None
                self.current_env = StockTradingEnv(
                    df=df, trigger_price=tp, direction=direction,
                    initial_capital=self.initial_capital, position_pct=self.position_pct,
                    split=self.split,
                    cache_dir=self._cache_dir,
                    external_features=external_df,
                    live_mode=self._live_mode
                )
                return self.current_env.reset(seed=effective_seed, options=options)

            def step(self, action):
                return self.current_env.step(action)

        def _make_env(rank: int):
            def _init():
                return UniversalStockEnv(datasets, initial_capital, position_pct, split,
                                         seed=rank, cache_dir=cache_dir, live_mode=live_mode)
            return _init

        env_fns = [_make_env(i) for i in range(n_envs)]
        if n_envs == 1:
            return DummyVecEnv(env_fns)
        return SubprocVecEnv(env_fns)

    def _make_ppo(self, env, **kwargs) -> PPO:
        """初始化 PPO，使用更保守的正则化超参数"""
        defaults = dict(
            learning_rate=3e-4,
            n_steps=2048,
            batch_size=128,
            n_epochs=5,
            gamma=0.99,
            gae_lambda=0.95,
            clip_range=0.2,
            verbose=0,
            policy_kwargs=dict(net_arch=dict(pi=[32, 32], vf=[32, 32]))
        )
        defaults.update(kwargs)
        return PPO("MlpPolicy", env, **defaults)

    def train(self, code: str, direction: str, df: pd.DataFrame,
              trigger_price: float, total_timesteps: int = 50000,
              initial_capital: float = 100000, position_pct: float = 0.2,
              eval_freq: int = 10000, patience: int = 2,
              external_features=None, live_mode: bool = False,
              **env_kwargs) -> Dict:
        """
        训练 PPO 模型（单股票），带验证集 Early Stopping。
        """
        # 优化：使用2个并行环境，平衡速度和稳定性
        n_envs = 2
        self.logger.info(
            f"🤖 开始训练 RL 模型: {code} ({direction}), "
            f"timesteps={total_timesteps}, n_envs={n_envs}, "
            f"external={'yes' if external_features is not None else 'no'}, live={live_mode}"
        )

        train_env = self.create_env(
            df, trigger_price, direction, initial_capital, position_pct,
            split='train', n_envs=n_envs,
            external_features=external_features, live_mode=live_mode,
            **env_kwargs
        )
        val_env = self.create_env(
            df, trigger_price, direction, initial_capital, position_pct,
            split='val', n_envs=1,
            external_features=external_features, live_mode=live_mode,
            **env_kwargs
        )

        model = self._make_ppo(train_env)
        callback = TrainingLoggerCallback(self.logger, log_interval=5000)

        best_sharpe = -np.inf
        best_path = None
        no_improve = 0

        steps_done = 0
        while steps_done < total_timesteps:
            steps = min(eval_freq, total_timesteps - steps_done)
            model.learn(total_timesteps=steps, callback=callback, progress_bar=False, reset_num_timesteps=False)
            steps_done += steps

            # 验证集评估
            val_result = self._evaluate(val_env, model, n_episodes=5)
            val_sharpe = val_result.get('sharpe', -np.inf)
            self.logger.info(
                f"[Val] step={steps_done} | return={val_result['avg_return']:.2%} | "
                f"sharpe={val_sharpe:.2f} | final_value={val_result['avg_final_value']:.0f}"
            )

            if val_sharpe > best_sharpe:
                best_sharpe = val_sharpe
                best_path = os.path.join(self.model_dir, f"ppo_{code}_{direction}_best.zip")
                model.save(best_path)
                no_improve = 0
            else:
                no_improve += 1
                if no_improve >= patience:
                    self.logger.info(f"⏹️ Early stopping at step {steps_done} (patience={patience})")
                    break

        # 加载最佳模型并保存为标准名
        model_path = self._get_model_path(code, direction)
        self._backup_model_if_exists(model_path)
        if best_path and os.path.exists(best_path):
            model = PPO.load(best_path)
            model.save(model_path)
            os.remove(best_path)
        else:
            model.save(model_path)

        self.models[f"{code}_{direction}"] = model

        # 最终训练集评估
        eval_result = self._evaluate(train_env, model, n_episodes=3)
        self.logger.info(
            f"✅ RL 模型训练完成: {code} | "
            f"train_return: {eval_result['avg_return']:.2%} | "
            f"best_val_sharpe: {best_sharpe:.2f}"
        )

        return {
            'success': True,
            'code': code,
            'direction': direction,
            'model_path': model_path,
            'timesteps': steps_done,
            'avg_return': eval_result['avg_return'],
            'avg_final_value': eval_result['avg_final_value'],
            'best_val_sharpe': best_sharpe
        }

    def train_universal(self, direction: str, datasets: List[Tuple],
                        total_timesteps: int = 100000, initial_capital: float = 100000,
                        position_pct: float = 0.2, eval_freq: int = 20000, patience: int = 2,
                        live_mode: bool = False) -> Dict:
        """
        训练通用策略（Market-Agnostic），在多个股票上联合训练一个 PPO 模型。

        :param direction: 'below' 或 'above'
        :param datasets: [(df, trigger_price, stock_code, external_df), ...]
                             external_df 可选
        """
        # 优化：使用2个并行环境，减少进程开销
        n_envs = 2
        self.logger.info(
            f"🌍 开始训练通用 RL 模型 ({direction}), "
            f"股票数={len(datasets)}, timesteps={total_timesteps}, n_envs={n_envs}, live={live_mode}"
        )

        train_env = self.create_universal_env(
            datasets, initial_capital, position_pct, split='train',
            n_envs=n_envs, live_mode=live_mode
        )
        val_env = self.create_universal_env(
            datasets, initial_capital, position_pct, split='val',
            n_envs=1, live_mode=live_mode
        )

        model = self._make_ppo(train_env)
        callback = TrainingLoggerCallback(self.logger, log_interval=10000)

        best_sharpe = -np.inf
        best_path = None
        no_improve = 0

        steps_done = 0
        while steps_done < total_timesteps:
            steps = min(eval_freq, total_timesteps - steps_done)
            model.learn(total_timesteps=steps, callback=callback, progress_bar=False, reset_num_timesteps=False)
            steps_done += steps

            val_result = self._evaluate(val_env, model, n_episodes=10)
            val_sharpe = val_result.get('sharpe', -np.inf)
            self.logger.info(
                f"[Val] step={steps_done} | return={val_result['avg_return']:.2%} | "
                f"sharpe={val_sharpe:.2f} | final_value={val_result['avg_final_value']:.0f}"
            )

            if val_sharpe > best_sharpe:
                best_sharpe = val_sharpe
                best_path = os.path.join(self.model_dir, f"ppo_universal_{direction}_best.zip")
                model.save(best_path)
                no_improve = 0
            else:
                no_improve += 1
                if no_improve >= patience:
                    self.logger.info(f"⏹️ Early stopping at step {steps_done} (patience={patience})")
                    break

        model_path = self._get_universal_model_path(direction)
        self._backup_model_if_exists(model_path)
        if best_path and os.path.exists(best_path):
            model = PPO.load(best_path)
            model.save(model_path)
            os.remove(best_path)
        else:
            model.save(model_path)

        self.models[f"universal_{direction}"] = model

        train_result = self._evaluate(train_env, model, n_episodes=5)
        self.logger.info(
            f"✅ 通用 RL 模型训练完成 ({direction}) | "
            f"train_return: {train_result['avg_return']:.2%} | "
            f"best_val_sharpe: {best_sharpe:.2f}"
        )

        return {
            'success': True,
            'direction': direction,
            'model_path': model_path,
            'timesteps': steps_done,
            'avg_return': train_result['avg_return'],
            'avg_final_value': train_result['avg_final_value'],
            'best_val_sharpe': best_sharpe
        }

    def _evaluate(self, env: DummyVecEnv, model: PPO, n_episodes: int = 3) -> Dict:
        """评估模型表现，返回收益率、最终市值、夏普比率。
        注意：env 必须是单环境 (n_envs=1) 以避免数组维度问题。"""
        returns = []
        final_values = []
        daily_returns_all = []

        for ep in range(n_episodes):
            obs = env.reset()
            # 单环境时 done 是布尔标量或长度为1的数组
            done = False
            initial_value = env.get_attr("initial_capital")[0]
            episode_values = [initial_value]
            step_count = 0
            max_steps = 1000  # 防止无限循环

            while not done and step_count < max_steps:
                action, _ = model.predict(obs, deterministic=True)
                obs, reward, done_flag, info = env.step(action)
                # 兼容单/多环境返回格式
                done = bool(done_flag[0]) if isinstance(done_flag, (list, np.ndarray)) else bool(done_flag)
                step_count += 1
                # 从 info 提取每日总市值
                try:
                    if info and len(info) > 0:
                        tv = info[0].get('total_value', initial_value)
                        episode_values.append(tv)
                except (IndexError, TypeError):
                    pass

            final_value = episode_values[-1]
            ret = (final_value - initial_value) / initial_value
            returns.append(ret)
            final_values.append(final_value)

            # 计算 episode 的 daily returns 以估算 sharpe
            if len(episode_values) > 1:
                dr = np.diff(episode_values) / episode_values[:-1]
                daily_returns_all.extend(dr.tolist())

        sharpe = 0.0
        if len(daily_returns_all) > 1:
            dr_arr = np.array(daily_returns_all)
            std = dr_arr.std()
            if std > 0:
                sharpe = (dr_arr.mean() / std) * np.sqrt(252)

        return {
            'avg_return': float(np.mean(returns)),
            'avg_final_value': float(np.mean(final_values)),
            'sharpe': float(sharpe)
        }

    def load_model(self, code: str, direction: str, universal: bool = False,
                   model_suffix: str = "") -> bool:
        """加载已保存的模型（线程安全）"""
        if universal:
            base_path = self._get_universal_model_path(direction)
            key = f"universal_{direction}"
        else:
            base_path = self._get_model_path(code, direction)
            key = f"{code}_{direction}"

        if model_suffix:
            # 在 .zip 前插入后缀
            model_path = base_path.replace('.zip', f'_{model_suffix}.zip')
            key = f"{key}_{model_suffix}"
        else:
            model_path = base_path

        if not os.path.exists(model_path):
            return False

        with self._model_lock:
            # 双重检查：避免多个线程重复加载
            if key in self.models:
                return True
            try:
                model = PPO.load(model_path)
                self.models[key] = model
                return True
            except Exception as e:
                self.logger.error(f"加载 RL 模型失败: {e}")
                return False

    def predict(self, code: str, direction: str, obs: np.ndarray,
                universal: bool = False, model_suffix: str = "") -> Optional[int]:
        """
        预测动作（线程安全，单进程内加锁串行化）。

        高并发部署建议：
        - 使用进程隔离（gunicorn --workers N 且每个 worker 独立持有 RLAgent 实例）
        - 或把 RL 推理拆成独立 gRPC/HTTP 微服务，队列式请求。

        Args:
            obs: 状态向量
            universal: 是否使用通用策略模型
            model_suffix: 模型文件名后缀（如 'ext'）

        Returns:
            动作编号 0-4
        """
        key = f"universal_{direction}" if universal else f"{code}_{direction}"
        if model_suffix:
            key = f"{key}_{model_suffix}"

        if key not in self.models:
            if not self.load_model(code, direction, universal=universal, model_suffix=model_suffix):
                return None

        with self._model_lock:
            model = self.models[key]
            obs_dim = model.observation_space.shape[0]
            if obs.shape[0] != obs_dim:
                if obs.shape[0] > obs_dim:
                    obs = obs[:obs_dim]
                else:
                    obs = np.pad(obs, (0, obs_dim - obs.shape[0]), mode='constant')
            action, _ = model.predict(obs, deterministic=True)
            return int(action)

    def train_all(self, targets: List[Dict], market_fetcher,
                  days: int = 120, timesteps: int = 50000,
                  universal: bool = True, use_external_features: bool = False) -> List[Dict]:
        """
        批量训练 RL 模型。

        如果 universal=True，会把所有股票的数据合并训练一个通用模型（按 direction 分组）。
        否则按单股票独立训练。

        use_external_features: 是否使用外部特征（北向资金等）
        """
        from rl_features import prepare_external_features

        results = []

        if universal:
            # 按 direction 分组，训练通用模型
            from collections import defaultdict
            by_direction = defaultdict(list)

            for target in targets:
                try:
                    df = market_fetcher.get_historical_data(target['code'], days=days)
                    if len(df) < 60:
                        self.logger.warning(f"{target['name']} 历史数据不足 ({len(df)} < 60)，跳过")
                        continue
                    external_df = prepare_external_features(df, target['code'], target.get('name')) if use_external_features else None
                    by_direction[target['direction']].append((df, target['trigger_price'], target['code'], external_df))
                except Exception as e:
                    self.logger.error(f"获取 {target['name']} 数据失败: {e}")

            for direction, datasets in by_direction.items():
                if not datasets:
                    continue
                try:
                    result = self.train_universal(
                        direction=direction,
                        datasets=datasets,
                        total_timesteps=timesteps * max(1, len(datasets) // 2),
                        live_mode=False
                    )
                    results.append(result)
                except Exception as e:
                    self.logger.error(f"通用 RL 训练 ({direction}) 失败: {e}")
        else:
            for target in targets:
                try:
                    df = market_fetcher.get_historical_data(target['code'], days=days)
                    if len(df) < 60:
                        self.logger.warning(f"{target['name']} 历史数据不足 ({len(df)} < 60)，跳过 RL 训练")
                        continue

                    external_df = prepare_external_features(df, target['code'], target.get('name')) if use_external_features else None
                    result = self.train(
                        code=target['code'],
                        direction=target['direction'],
                        df=df,
                        trigger_price=target['trigger_price'],
                        total_timesteps=timesteps,
                        external_features=external_df
                    )
                    results.append(result)

                except Exception as e:
                    self.logger.error(f"RL 训练 {target['name']} 时出错: {e}")

        return results
