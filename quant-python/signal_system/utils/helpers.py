"""工具函数模块.

提供配置加载、默认值合并和基础持久化能力。
"""

from copy import deepcopy
from datetime import datetime
import logging
import os
import re

import yaml


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(value):
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match):
        name, default = match.group(1), match.group(2) or ''
        return os.getenv(name, default)

    return _ENV_PATTERN.sub(replace, value)


def _resolve_runtime_paths(config, config_path):
    config_file = os.path.abspath(config_path)
    config_dir = os.path.dirname(config_file)
    base_dir = os.path.dirname(config_dir) if os.path.basename(config_dir) == 'config' else config_dir
    path_locations = [
        ('market_data', 'cache_dir'),
        ('data_source', 'cache_dir'),
        ('data', 'cache_dir'),
        ('runtime', 'database_path'),
        ('runtime', 'output_dir'),
    ]
    for section, key in path_locations:
        value = config.get(section, {}).get(key)
        if value and not os.path.isabs(value):
            config[section][key] = os.path.normpath(os.path.join(base_dir, value))
    log_file = config.get('runtime', {}).get('logging', {}).get('file')
    if log_file and not os.path.isabs(log_file):
        config['runtime']['logging']['file'] = os.path.normpath(os.path.join(base_dir, log_file))
    config['_config_path'] = config_file
    config['_base_dir'] = base_dir
    return config


DEFAULT_CONFIG = {
    "data_source": {
        "tushare_token": "",
        "use_cache": True,
        "cache_dir": "./cache",
    },
    "data": {
        "provider": "akshare",
        "minute_provider": "pytdx",
        "fallback_provider": "",
        "minute_fallback_provider": "",
        "disable_system_proxy": True,
        "request_workers": 4,
        "cache_dir": "./cache",
        "daily_cache_hours": 24,
        "minute_cache_hours": 6,
        "fundamentals_cache_hours": 168,
        "pytdx_host": "180.153.18.170",
        "pytdx_port": 7709,
        "pytdx_hosts": [
            "180.153.18.170:7709",
            "119.147.212.81:7709",
            "114.80.63.12:7709",
        ],
    },
    "strategy": {
        "fundamental": {
            "min_roe": 10,
            "max_debt_ratio": 60,
            "max_pe": 30,
            "min_market_cap": 50,
            "max_market_cap": 3000,
        },
        "technical": {
            "ma_period": 250,
            "macd_fast": 12,
            "macd_slow": 26,
            "macd_signal": 9,
            "ma250_distance_threshold": 0.05,
        },
        "volume": {
            "min_turnover_rate": 0.5,
            "max_turnover_rate": 5,
            "volume_burst_ratio": 0.8,
        },
        "macd": {
            "pullback_confirmation_bars": 5,
            "long_ma_period": 250,
            "position_lookback": 20,
            "max_long_ma_distance": 0.35,
            "max_recent_return": 0.30,
            "high_position_volume_ratio": 3.0,
        },
        "enabled": {
            "trend_following": True,
            "mean_reversion": False,
            "breakout": False,
        },
        "minute_frames": [5, 30, 60],
        "candidate_pool_size": 30,
    },
    "selector": {
        "pe_excellent_max": 17,
        "near_ma_threshold": 0.05,
        "price_change_soft_min": -0.03,
        "price_change_soft_max": 0.03,
    },
    "stock_pool": {
        "enabled": True,
        "min_market_cap": 50,
        "max_market_cap": 3000,
        "amount_window": 20,
        "min_avg_amount": 1.0,
        "turnover_window": 20,
        "min_avg_turnover_rate": 0.5,
        "max_avg_turnover_rate": 8.0,
        "min_listing_trade_days": 120,
        "exclude_st": True,
        "exclude_delisting": True,
        "missing_data_policy": "reject",
    },
    "entry_filters": {
        "market_gate_enabled": True,
        "market_index_code": "000001.SH",
        "market_gate_fail_open": False,
        "position_gate_enabled": True,
    },
    "regime": {
        "mode": "auto",
        "index_code": "000001.SH",
        "ma_short": 20,
        "ma_long": 250,
        "slope_window": 20,
        "lookback_bars": 300,
        "bull_score_threshold": 0.70,
        "bear_score_threshold": 0.70,
        "range_score_threshold": 0.60,
    },
    "position": {
        "min_stocks": 2,
        "target_stocks": 3,
        "max_stocks": 4,
        "base_position_per_stock": 0.25,
        "mobile_cash_ratio": 0.25,
        "max_position_per_stock": 0.40,
    },
    "risk": {
        "stop_loss_pct": 0.08,
        "stop_profit_pct": 0.30,
        "long_holding_days_threshold": 40,
        "long_holding_stop_loss_multiplier": 1.25,
        "long_holding_stop_profit_multiplier": 1.10,
        "high_volatility_threshold_pct": 0.35,
        "high_volatility_stop_loss_multiplier": 1.20,
        "high_volatility_stop_profit_multiplier": 1.10,
        "max_portfolio_drawdown_pct": 0.20,
        "max_single_day_drawdown_pct": 0.02,
        "allow_new_position_when_drawdown_exceeded": False,
    },
    "t_trading": {
        "enabled": True,
        "positive_t_step_pct": 0.05,
        "negative_t_step_pct": 0.05,
        "range_t_step_pct": 0.05,
    },
    "backtest": {
        "initial_cash": 100000,
        "adjustment": "qfq",
        "history_bars": 800,
        "fetch_missing_adjusted": True,
        "commission_pct": 0.0003,
        "minimum_commission": 5.0,
        "stamp_tax_pct": 0.001,
        "slippage_pct": 0.0005,
        "lot_size": 100,
        "t_plus_one": True,
        "price_limit_model": "conservative",
        "st_symbols": [],
        "intrabar_conflict": "stop_first",
        "market_gate_slope_bars": 5,
        "signal_priority": [
            "macd_golden_cross_pullback_confirmed_above",
            "macd_golden_cross_pullback_confirmed_near",
            "buy_1",
            "buy_2",
            "buy_3",
        ],
        "chan_zero_axis": {
            "allowed_zones": ["above", "near"],
            "min_confirmations": 0,
            "cross_window_bars": 5,
            "max_holding_bars": 40,
        },
    },
    "manual_overrides": {
        "regime_override": "auto",
        "regime_override_reason": "",
        "disable_new_positions": False,
        "only_reduce_positions": False,
        "max_total_exposure": 1.0,
        "watchlist_only": [],
    },
    "risk_control": {
        "position": {
            "max_total_position": 0.8,
            "max_single_position": 0.25,
            "reserve_cash_ratio": 0.25,
        },
        "market_position": {
            "bull_market": 0.8,
            "sideways_market": 0.5,
            "range_market": 0.5,
            "bear_market": 0.2,
        },
        "stop_loss": 0.08,
        "stop_profit": 0.30,
        "max_drawdown": 0.15,
    },
    "notification": {
        "wechat": {
            "enabled": True,
            "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY",
        },
        "email": {
            "enabled": False,
            "smtp_server": "smtp.qq.com",
            "smtp_port": 465,
            "sender": "your@email.com",
            "password": "your_password",
            "receiver": "your@email.com",
        },
        "bark": {
            "enabled": False,
            "url": "https://api.day.app/push",
            "device_key": "",
        },
        "channels": ["wecom", "email", "bark"],
        "push_market_regime": True,
        "push_candidate_pool": True,
        "push_trade_signal": True,
        "push_ai_analysis": True,
    },
    "runtime": {
        "schedule": {
            "daily_scan_time": "04:00",
            "weekly_review_day": "Friday",
        },
        "logging": {
            "level": "INFO",
            "file": "./logs/signal_system.log",
        },
        "database": {
            "enabled": False,
            "type": "sqlite",
            "path": "./state/signals.db",
        },
    },
}


def load_config(config_path='config/config.yaml'):
    """
    加载配置文件

    Args:
        config_path: str, 配置文件路径

    Returns:
        dict: 配置字典
    """
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            user_config = yaml.safe_load(f) or {}
        config = _deep_merge(deepcopy(DEFAULT_CONFIG), _expand_env(user_config))
        config = _resolve_runtime_paths(config, config_path)
        return _normalize_compatibility_sections(config)
    except Exception as e:
        print(f"加载配置文件失败: {e}")
        return None


def _deep_merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _normalize_compatibility_sections(config):
    if not config.get("data", {}).get("cache_dir"):
        config["data"]["cache_dir"] = config["data_source"]["cache_dir"]
    if not config.get("data_source", {}).get("cache_dir"):
        config["data_source"]["cache_dir"] = config["data"]["cache_dir"]

    config["risk_control"]["position"]["max_single_position"] = config["position"]["base_position_per_stock"]
    config["risk_control"]["position"]["reserve_cash_ratio"] = config["position"]["mobile_cash_ratio"]
    config["risk_control"]["stop_loss"] = config["risk"]["stop_loss_pct"]
    config["risk_control"]["stop_profit"] = config["risk"]["stop_profit_pct"]

    return config


def setup_logging(config):
    """
    初始化日志系统

    Args:
        config: dict, 配置字典
    """
    log_config = config.get('runtime', {}).get('logging', {})
    log_level = log_config.get('level', 'INFO')
    log_file = log_config.get('file', './logs/signal_system.log')

    log_dir = os.path.dirname(log_file)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir)

    logging.basicConfig(
        level=getattr(logging, log_level),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )


def save_signal_history(scan_result, output_dir='./output'):
    """
    保存信号历史

    Args:
        scan_result: dict, 扫描结果
        output_dir: str, 输出目录
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = os.path.join(output_dir, f'signals_{timestamp}.yaml')

    try:
        with open(filename, 'w', encoding='utf-8') as f:
            yaml.dump(scan_result, f, allow_unicode=True, default_flow_style=False)
        logging.info(f"信号历史已保存: {filename}")
    except Exception as e:
        logging.error(f"保存信号历史失败: {e}")


def load_positions(positions_file='./state/positions.yaml'):
    """
    加载持仓数据

    Args:
        positions_file: str, 持仓文件路径

    Returns:
        list: 持仓列表
    """
    if not os.path.exists(positions_file):
        return []

    try:
        with open(positions_file, 'r', encoding='utf-8') as f:
            positions = yaml.safe_load(f)
        return positions if positions else []
    except Exception as e:
        logging.error(f"加载持仓数据失败: {e}")
        return []


def save_positions(positions, positions_file='./state/positions.yaml'):
    """
    保存持仓数据

    Args:
        positions: list, 持仓列表
        positions_file: str, 持仓文件路径
    """
    positions_dir = os.path.dirname(positions_file)
    if positions_dir and not os.path.exists(positions_dir):
        os.makedirs(positions_dir)

    try:
        with open(positions_file, 'w', encoding='utf-8') as f:
            yaml.dump(positions, f, allow_unicode=True, default_flow_style=False)
        logging.info(f"持仓数据已保存: {positions_file}")
    except Exception as e:
        logging.error(f"保存持仓数据失败: {e}")
