"""
工具函数模块
配置加载、日志初始化等
"""

import yaml
import logging
import os
from datetime import datetime


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
            config = yaml.safe_load(f)
        return config
    except Exception as e:
        print(f"加载配置文件失败: {e}")
        return None


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


def load_positions(positions_file='./data/positions.yaml'):
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


def save_positions(positions, positions_file='./data/positions.yaml'):
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
