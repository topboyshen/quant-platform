#!/usr/bin/env python3
"""
机器学习预测模块
基于历史数据预测价格触发概率
"""

import json
import logging
import os
import pickle
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import accuracy_score, precision_score, recall_score
from sklearn.preprocessing import StandardScaler


class FeatureEngineer:
    """特征工程：从价格数据中提取特征"""

    @staticmethod
    def calculate_returns(prices: List[float], periods: List[int] = [1, 5, 10, 20]) -> Dict[str, float]:
        """计算不同周期的收益率"""
        features = {}
        for period in periods:
            if len(prices) > period:
                ret = (prices[-1] - prices[-period-1]) / prices[-period-1] * 100
                features[f'return_{period}d'] = ret
            else:
                features[f'return_{period}d'] = 0
        return features

    @staticmethod
    def calculate_volatility(prices: List[float], window: int = 20) -> float:
        """计算波动率"""
        if len(prices) < window:
            return 0
        returns = np.diff(prices) / prices[:-1] * 100
        return np.std(returns[-window:]) if len(returns) >= window else 0

    @staticmethod
    def calculate_rsi(prices: List[float], period: int = 14) -> float:
        """计算RSI指标"""
        if len(prices) < period + 1:
            return 50

        deltas = np.diff(prices)
        gains = deltas[deltas > 0]
        losses = -deltas[deltas < 0]

        avg_gain = np.mean(gains[-period:]) if len(gains) > 0 else 0
        avg_loss = np.mean(losses[-period:]) if len(losses) > 0 else 0.001

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    @staticmethod
    def calculate_momentum(prices: List[float], volumes: List[float] = None) -> Dict[str, float]:
        """计算动量指标"""
        if len(prices) < 20:
            return {'momentum_10': 0, 'momentum_20': 0, 'volume_trend': 0}

        momentum_10 = (prices[-1] - prices[-10]) / prices[-10] * 100
        momentum_20 = (prices[-1] - prices[-20]) / prices[-20] * 100

        volume_trend = 0
        if volumes and len(volumes) >= 10:
            recent_vol = np.mean(volumes[-5:])
            past_vol = np.mean(volumes[-10:-5])
            volume_trend = (recent_vol - past_vol) / past_vol * 100 if past_vol > 0 else 0

        return {
            'momentum_10': momentum_10,
            'momentum_20': momentum_20,
            'volume_trend': volume_trend
        }

    @staticmethod
    def extract_features(price_history: List[float], volume_history: List[float] = None) -> Dict[str, float]:
        """提取完整特征集"""
        if len(price_history) < 30:
            return None

        features = {}

        # 收益率特征
        features.update(FeatureEngineer.calculate_returns(price_history))

        # 波动率
        features['volatility_20d'] = FeatureEngineer.calculate_volatility(price_history, 20)
        features['volatility_10d'] = FeatureEngineer.calculate_volatility(price_history, 10)

        # RSI
        features['rsi_14'] = FeatureEngineer.calculate_rsi(price_history, 14)
        features['rsi_7'] = FeatureEngineer.calculate_rsi(price_history, 7)

        # 动量
        momentum = FeatureEngineer.calculate_momentum(price_history, volume_history)
        features.update(momentum)

        # 价格位置（相对于近期高低点）
        high_20 = max(price_history[-20:])
        low_20 = min(price_history[-20:])
        current = price_history[-1]
        features['price_position'] = (current - low_20) / (high_20 - low_20) * 100 if high_20 != low_20 else 50

        # 均线偏离
        ma5 = np.mean(price_history[-5:])
        ma10 = np.mean(price_history[-10:])
        ma20 = np.mean(price_history[-20:])
        features['ma5_deviation'] = (current - ma5) / ma5 * 100
        features['ma10_deviation'] = (current - ma10) / ma10 * 100
        features['ma20_deviation'] = (current - ma20) / ma20 * 100
        features['ma5_ma10_diff'] = (ma5 - ma10) / ma10 * 100

        return features


class MLPredictor:
    """机器学习预测器"""

    def __init__(self, model_dir: str = "models", logger: Optional[logging.Logger] = None):
        self.model_dir = model_dir
        self.logger = logger or logging.getLogger(__name__)
        self.models: Dict[str, any] = {}
        self.scalers: Dict[str, StandardScaler] = {}
        self.feature_names: List[str] = []
        # 每个 (code, direction) 对应的特征列顺序（与训练时 scaler 一致）
        self._feature_columns: Dict[str, List[str]] = {}
        # 文件不存在或加载失败：跳过后续重复 I/O（新文件需重启或重新训练以清除）
        self._unloadable_keys: Set[str] = set()

        os.makedirs(model_dir, exist_ok=True)

    def _get_model_path(self, code: str, direction: str) -> str:
        """获取模型文件路径"""
        return os.path.join(self.model_dir, f"{code}_{direction}_model.pkl")

    def _get_scaler_path(self, code: str, direction: str) -> str:
        """获取缩放器文件路径"""
        return os.path.join(self.model_dir, f"{code}_{direction}_scaler.pkl")

    def _get_features_meta_path(self, code: str, direction: str) -> str:
        """特征列顺序元数据（与旧版 pkl 兼容：无此文件时从 scaler 推断）"""
        return os.path.join(self.model_dir, f"{code}_{direction}_features.json")

    def prepare_training_data(self, df: pd.DataFrame, trigger_price: float,
                             direction: str, prediction_window: int = 5) -> Tuple[pd.DataFrame, pd.Series]:
        """
        准备训练数据

        标签定义：
        - 在未来 prediction_window 个周期内触发目标价格 = 1
        - 否则 = 0
        """
        prices = df['price'].tolist()
        volumes = df['volume'].tolist() if 'volume' in df.columns else None

        X = []
        y = []

        for i in range(30, len(prices) - prediction_window):
            # 提取特征
            price_window = prices[:i+1]
            volume_window = volumes[:i+1] if volumes else None

            features = FeatureEngineer.extract_features(price_window, volume_window)
            if features is None:
                continue

            # 判断未来是否触发
            future_prices = prices[i+1:i+1+prediction_window]
            triggered = False

            if direction == 'below':
                triggered = any(p <= trigger_price for p in future_prices)
            else:  # above
                triggered = any(p >= trigger_price for p in future_prices)

            X.append(features)
            y.append(1 if triggered else 0)

        if not X:
            return None, None

        X_df = pd.DataFrame(X)
        self.feature_names = list(X_df.columns)

        return X_df, pd.Series(y)

    def _split_data(self, X, y):
        """按时间顺序拆分训练/验证集，避免金融时间序列随机切分泄漏。"""
        positive_count = int(sum(y))
        if positive_count == 0:
            self.logger.warning("没有正样本（触发事件），无法训练有效模型")
            return None

        n = len(y)
        if n < 10:
            self.logger.warning("样本过少，无法做可靠时间序列验证")
            return None

        split_idx = max(int(n * 0.8), 1)
        split_idx = min(split_idx, n - 1)
        X_train, X_test = X.iloc[:split_idx], X.iloc[split_idx:]
        y_train, y_test = y.iloc[:split_idx], y.iloc[split_idx:]

        if sum(y_train) == 0:
            self.logger.warning("时间序列训练集没有正样本，无法训练有效模型")
            return None
        if sum(y_test) < 3:
            self.logger.warning(f"时间序列测试集正样本过少 ({int(sum(y_test))}个)，指标可能不可靠")

        return X_train, X_test, y_train, y_test

    def _build_and_fit_model(self, X_train, y_train):
        """构建并拟合梯度提升模型。"""
        model = GradientBoostingClassifier(
            n_estimators=100, max_depth=5, learning_rate=0.1, random_state=42
        )
        model.fit(X_train, y_train)
        return model

    def _evaluate_model(self, model, X_test, y_test):
        """评估模型并返回指标字典。"""
        y_pred = model.predict(X_test)

        # 检查测试集正样本数
        positive_in_test = sum(y_test)
        if positive_in_test < 3:
            self.logger.warning(f"测试集正样本过少 ({positive_in_test}个)，指标可能不可靠")

        return {
            "accuracy": accuracy_score(y_test, y_pred),
            "precision": precision_score(y_test, y_pred, zero_division=0),
            "recall": recall_score(y_test, y_pred, zero_division=0),
        }

    def _persist_model(self, code, direction, model, scaler, feature_cols):
        """持久化模型、scaler 和特征元数据。"""
        model_path = self._get_model_path(code, direction)
        scaler_path = self._get_scaler_path(code, direction)

        with open(model_path, "wb") as f:
            pickle.dump(model, f)
        with open(scaler_path, "wb") as f:
            pickle.dump(scaler, f)

        key = f"{code}_{direction}"
        self._unloadable_keys.discard(key)
        self.models[key] = model
        self.scalers[key] = scaler
        self._feature_columns[key] = feature_cols
        self.feature_names = feature_cols

        try:
            with open(self._get_features_meta_path(code, direction), "w", encoding="utf-8") as f:
                json.dump({"feature_names": feature_cols}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.warning(f"写入特征元数据失败: {e}")

    def train(self, code: str, direction: str, df: pd.DataFrame,
              trigger_price: float, prediction_window: int = 5) -> Dict:
        """训练模型。"""
        self.logger.info(f"开始训练 {code} ({direction}) 的预测模型...")

        X, y = self.prepare_training_data(df, trigger_price, direction, prediction_window)
        if X is None or len(X) < 50:
            self.logger.warning(f"{code} 数据不足，无法训练模型")
            return {"success": False, "reason": "insufficient_data"}

        self.logger.info(f"训练样本数: {len(X)}, 正样本: {sum(y)}, 负样本: {len(y)-sum(y)}")

        split_result = self._split_data(X, y)
        if split_result is None:
            return {"success": False, "reason": "no_positive_samples"}
        X_train, X_test, y_train, y_test = split_result

        scaler = StandardScaler()
        X_train_scaled = scaler.fit_transform(X_train)
        X_test_scaled = scaler.transform(X_test)

        model = self._build_and_fit_model(X_train_scaled, y_train)
        metrics = self._evaluate_model(model, X_test_scaled, y_test)

        self.logger.info(
            f"训练完成 - 准确率: {metrics['accuracy']:.2%}, "
            f"精确率: {metrics['precision']:.2%}, 召回率: {metrics['recall']:.2%}"
        )

        self._persist_model(code, direction, model, scaler, list(X.columns))

        result = {
            "success": True,
            "samples": len(X),
            "positive_samples": int(sum(y)),
            "validation_method": "chronological_80_20",
            **metrics,
        }
        try:
            from radar.model_manifest import record_ml_training_run
            record_ml_training_run(
                model_dir=self.model_dir, code=code, direction=direction,
                prediction_window=prediction_window, df=df, train_result=result,
            )
        except Exception as e:
            self.logger.warning(f"更新 models/manifest.json 失败: {e}")

        return result

    def load_model(self, code: str, direction: str) -> bool:
        """加载已保存的模型"""
        key = f"{code}_{direction}"
        if key in self._unloadable_keys:
            return False
        model_path = self._get_model_path(code, direction)
        scaler_path = self._get_scaler_path(code, direction)

        if not os.path.exists(model_path) or not os.path.exists(scaler_path):
            self._unloadable_keys.add(key)
            return False

        try:
            with open(model_path, 'rb') as f:
                self.models[key] = pickle.load(f)

            with open(scaler_path, 'rb') as f:
                self.scalers[key] = pickle.load(f)

            meta_path = self._get_features_meta_path(code, direction)
            if os.path.isfile(meta_path):
                with open(meta_path, 'r', encoding='utf-8') as f:
                    self._feature_columns[key] = json.load(f)['feature_names']
            else:
                scaler = self.scalers[key]
                names = getattr(scaler, 'feature_names_in_', None)
                if names is not None:
                    self._feature_columns[key] = [str(x) for x in names]

            self._unloadable_keys.discard(key)
            return True
        except Exception as e:
            self.logger.error(f"加载模型失败: {e}")
            self._unloadable_keys.add(key)
            return False

    def predict(self, code: str, direction: str, price_history: List[float],
                volume_history: List[float] = None) -> Optional[Dict]:
        """
        预测触发概率

        Returns:
            {
                'probability': 触发概率 (0-1),
                'will_trigger': 是否预测会触发 (bool),
                'confidence': 置信度 (high/medium/low),
                'features': 使用的特征值
            }
        """
        key = f"{code}_{direction}"

        if key not in self.models:
            if not self.load_model(code, direction):
                return None

        # 提取特征
        features = FeatureEngineer.extract_features(price_history, volume_history)
        if features is None:
            return None

        # 准备输入
        X = pd.DataFrame([features])

        cols = self._feature_columns.get(key)
        if not cols and self.feature_names:
            cols = self.feature_names
        if cols:
            try:
                X = X[cols]
            except KeyError as e:
                self.logger.error(f"{key} 特征列与模型不一致: {e}")
                return None

        # 标准化
        scaler = self.scalers[key]
        try:
            X_scaled = scaler.transform(X)
        except Exception as e:
            self.logger.error(f"{key} scaler.transform 失败: {e}")
            return None

        # 预测
        model = self.models[key]
        proba = model.predict_proba(X_scaled)[0]

        # 解析结果（兼容单类标签等边界情况）
        if len(proba) > 1:
            trigger_prob = float(proba[1])
        else:
            trigger_prob = float(proba[0])
        will_trigger = trigger_prob > 0.5

        # 置信度分级
        if trigger_prob > 0.8 or trigger_prob < 0.2:
            confidence = 'high'
        elif trigger_prob > 0.65 or trigger_prob < 0.35:
            confidence = 'medium'
        else:
            confidence = 'low'

        return {
            'probability': float(trigger_prob),
            'will_trigger': bool(will_trigger),
            'confidence': confidence,
            'features': features
        }

    def get_feature_importance(self, code: str, direction: str) -> Optional[Dict[str, float]]:
        """获取特征重要性"""
        key = f"{code}_{direction}"

        if key not in self.models:
            if not self.load_model(code, direction):
                return None

        model = self.models[key]

        if hasattr(model, 'feature_importances_'):
            names = self._feature_columns.get(key) or self.feature_names
            imp = model.feature_importances_
            if names and len(names) == len(imp):
                importance = dict(zip(names, imp))
            else:
                importance = {f'f{i}': float(v) for i, v in enumerate(imp)}
            return dict(sorted(importance.items(), key=lambda x: x[1], reverse=True))

        return None
