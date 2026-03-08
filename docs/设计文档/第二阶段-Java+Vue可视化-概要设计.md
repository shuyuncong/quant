# 量化交易信号系统 - 第二阶段概要设计

**文档版本**: v1.0
**编写日期**: 2026-02-11
**系统阶段**: 第二阶段 - Java + Vue 可视化系统

---

## 1. 系统概述

### 1.1 系统定位

第二阶段在第一阶段 Python 核心引擎的基础上，构建**可视化管理和回测分析系统**，核心目标是：
- 提供 Web 界面查看信号和管理持仓
- 实现策略回测和绩效分析
- 数据持久化和历史查询
- 参数配置和策略管理

### 1.2 核心特性

- ✅ **信号看板**: 实时查看买入/卖出信号
- ✅ **持仓管理**: 记录和管理持仓信息
- ✅ **回测引擎**: 历史数据回测验证策略
- ✅ **绩效分析**: 收益率、胜率、盈亏比等指标
- ✅ **参数配置**: Web 界面配置策略参数
- ✅ **数据可视化**: K 线图、指标图、收益曲线

### 1.3 与第一阶段的关系

```
┌──────────────────────────────────────┐
│   第一阶段: Python 核心引擎           │
│   - 数据获取                          │
│   - 策略计算                          │
│   - 信号生成                          │
└──────────────┬───────────────────────┘
               │ REST API / 文件
               ↓
┌──────────────────────────────────────┐
│   第二阶段: Java + Vue 可视化系统     │
│   - Web 界面                          │
│   - 数据管理                          │
│   - 回测分析                          │
└──────────────────────────────────────┘
```

---

## 2. 系统架构

### 2.1 总体架构

```
┌─────────────────────────────────────────────────────────┐
│                    浏览器 (Browser)                      │
└────────────────────┬────────────────────────────────────┘
                     │ HTTP/WebSocket
┌────────────────────▼────────────────────────────────────┐
│                  Vue 前端 (Frontend)                     │
│  ┌──────────┬──────────┬──────────┬──────────┐         │
│  │ 信号看板 │ 持仓管理 │ 回测分析 │ 参数配置 │         │
│  └──────────┴──────────┴──────────┴──────────┘         │
└────────────────────┬────────────────────────────────────┘
                     │ REST API
┌────────────────────▼────────────────────────────────────┐
│              Java 后端 (Spring Boot)                     │
│  ┌──────────────────────────────────────────────┐       │
│  │  Controller 层 (API 接口)                    │       │
│  └──────────────────┬───────────────────────────┘       │
│  ┌──────────────────▼───────────────────────────┐       │
│  │  Service 层 (业务逻辑)                       │       │
│  │  - SignalService                             │       │
│  │  - PositionService                           │       │
│  │  - BacktestService                           │       │
│  │  - StrategyService                           │       │
│  └──────────────────┬───────────────────────────┘       │
│  ┌──────────────────▼───────────────────────────┐       │
│  │  Repository 层 (数据访问)                    │       │
│  └──────────────────┬───────────────────────────┘       │
└─────────────────────┼───────────────────────────────────┘
                      │
        ┌─────────────┼─────────────┐
        │             │             │
   ┌────▼────┐   ┌───▼────┐   ┌───▼────────┐
   │ MySQL   │   │ Redis  │   │ Python 引擎│
   │ 数据库  │   │ 缓存   │   │ (第一阶段) │
   └─────────┘   └────────┘   └────────────┘
```

### 2.2 技术栈

#### 2.2.1 前端技术栈

| 技术 | 版本 | 用途 |
|------|------|------|
| Vue.js | 3.x | 前端框架 |
| Vue Router | 4.x | 路由管理 |
| Pinia | 2.x | 状态管理 |
| Element Plus | 2.x | UI 组件库 |
| ECharts | 5.x | 数据可视化 |
| Axios | 1.x | HTTP 客户端 |
| Day.js | 1.x | 日期处理 |

#### 2.2.2 后端技术栈

| 技术 | 版本 | 用途 |
|------|------|------|
| Spring Boot | 2.7.x | 后端框架 |
| Spring Data JPA | 2.7.x | ORM 框架 |
| MySQL | 8.0+ | 关系数据库 |
| Redis | 6.x | 缓存 |
| MyBatis Plus | 3.5.x | 持久层框架 |
| Lombok | 1.18.x | 代码简化 |
| Swagger | 3.x | API 文档 |

---

## 3. 数据库设计

### 3.1 核心表结构

#### 3.1.1 信号表 (t_signal)

```sql
CREATE TABLE t_signal (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    ts_code VARCHAR(20) NOT NULL COMMENT '股票代码',
    stock_name VARCHAR(50) COMMENT '股票名称',
    signal_type VARCHAR(10) NOT NULL COMMENT '信号类型: BUY/SELL',
    signal_time DATETIME NOT NULL COMMENT '信号时间',
    price DECIMAL(10,2) COMMENT '信号价格',
    timeframe VARCHAR(10) COMMENT '时间周期: 5m/30m/60m/120m/1d',
    strategy_name VARCHAR(50) COMMENT '策略名称',
    score INT COMMENT '信号评分',
    reason TEXT COMMENT '信号原因',
    indicators JSON COMMENT '技术指标快照',
    status VARCHAR(10) DEFAULT 'ACTIVE' COMMENT '状态: ACTIVE/EXPIRED/EXECUTED',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_ts_code (ts_code),
    INDEX idx_signal_time (signal_time),
    INDEX idx_status (status)
) COMMENT='交易信号表';
```

#### 3.1.2 持仓表 (t_position)

```sql
CREATE TABLE t_position (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    ts_code VARCHAR(20) NOT NULL COMMENT '股票代码',
    stock_name VARCHAR(50) COMMENT '股票名称',
    buy_price DECIMAL(10,2) NOT NULL COMMENT '买入价格',
    buy_date DATE NOT NULL COMMENT '买入日期',
    buy_signal_id BIGINT COMMENT '关联的买入信号ID',
    quantity INT NOT NULL COMMENT '持仓数量',
    cost_amount DECIMAL(15,2) COMMENT '成本金额',
    current_price DECIMAL(10,2) COMMENT '当前价格',
    market_value DECIMAL(15,2) COMMENT '市值',
    profit_loss DECIMAL(15,2) COMMENT '浮动盈亏',
    profit_rate DECIMAL(10,4) COMMENT '收益率',
    status VARCHAR(10) DEFAULT 'HOLDING' COMMENT '状态: HOLDING/CLOSED',
    sell_price DECIMAL(10,2) COMMENT '卖出价格',
    sell_date DATE COMMENT '卖出日期',
    sell_signal_id BIGINT COMMENT '关联的卖出信号ID',
    holding_days INT COMMENT '持仓天数',
    notes TEXT COMMENT '备注',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_ts_code (ts_code),
    INDEX idx_status (status),
    INDEX idx_buy_date (buy_date)
) COMMENT='持仓表';
```

#### 3.1.3 回测记录表 (t_backtest)

```sql
CREATE TABLE t_backtest (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    backtest_name VARCHAR(100) NOT NULL COMMENT '回测名称',
    strategy_name VARCHAR(50) NOT NULL COMMENT '策略名称',
    start_date DATE NOT NULL COMMENT '回测开始日期',
    end_date DATE NOT NULL COMMENT '回测结束日期',
    initial_capital DECIMAL(15,2) NOT NULL COMMENT '初始资金',
    final_capital DECIMAL(15,2) COMMENT '最终资金',
    total_return DECIMAL(10,4) COMMENT '总收益率',
    annual_return DECIMAL(10,4) COMMENT '年化收益率',
    max_drawdown DECIMAL(10,4) COMMENT '最大回撤',
    sharpe_ratio DECIMAL(10,4) COMMENT '夏普比率',
    win_rate DECIMAL(10,4) COMMENT '胜率',
    profit_loss_ratio DECIMAL(10,4) COMMENT '盈亏比',
    total_trades INT COMMENT '总交易次数',
    win_trades INT COMMENT '盈利次数',
    loss_trades INT COMMENT '亏损次数',
    config JSON COMMENT '回测配置',
    result_detail JSON COMMENT '详细结果',
    status VARCHAR(10) DEFAULT 'RUNNING' COMMENT '状态: RUNNING/COMPLETED/FAILED',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_strategy (strategy_name),
    INDEX idx_status (status)
) COMMENT='回测记录表';
```

#### 3.1.4 策略配置表 (t_strategy_config)

```sql
CREATE TABLE t_strategy_config (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    strategy_name VARCHAR(50) NOT NULL UNIQUE COMMENT '策略名称',
    strategy_type VARCHAR(20) COMMENT '策略类型',
    enabled BOOLEAN DEFAULT TRUE COMMENT '是否启用',
    weight DECIMAL(5,2) COMMENT '权重',
    params JSON NOT NULL COMMENT '策略参数',
    description TEXT COMMENT '策略描述',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) COMMENT='策略配置表';
```

#### 3.1.5 系统日志表 (t_system_log)

```sql
CREATE TABLE t_system_log (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    log_level VARCHAR(10) NOT NULL COMMENT '日志级别',
    module VARCHAR(50) COMMENT '模块名称',
    message TEXT COMMENT '日志消息',
    detail TEXT COMMENT '详细信息',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_level (log_level),
    INDEX idx_created_at (created_at)
) COMMENT='系统日志表';
```

---

## 4. API 设计

### 4.1 信号管理 API

#### 4.1.1 获取信号列表

```
GET /api/signals
Query Parameters:
  - page: 页码
  - size: 每页数量
  - signalType: 信号类型 (BUY/SELL)
  - status: 状态
  - startDate: 开始日期
  - endDate: 结束日期

Response:
{
  "code": 200,
  "message": "success",
  "data": {
    "total": 100,
    "list": [
      {
        "id": 1,
        "tsCode": "000001.SZ",
        "stockName": "平安银行",
        "signalType": "BUY",
        "signalTime": "2026-02-11 10:30:00",
        "price": 12.50,
        "timeframe": "30m",
        "strategyName": "trend_following",
        "score": 85,
        "reason": "底背离+放量",
        "status": "ACTIVE"
      }
    ]
  }
}
```

#### 4.1.2 获取信号详情

```
GET /api/signals/{id}

Response:
{
  "code": 200,
  "data": {
    "id": 1,
    "tsCode": "000001.SZ",
    "stockName": "平安银行",
    "signalType": "BUY",
    "signalTime": "2026-02-11 10:30:00",
    "price": 12.50,
    "timeframe": "30m",
    "strategyName": "trend_following",
    "score": 85,
    "reason": "底背离+放量",
    "indicators": {
      "macd": 0.15,
      "ma250": 12.30,
      "rsi": 45.5
    },
    "status": "ACTIVE"
  }
}
```

### 4.2 持仓管理 API

#### 4.2.1 获取持仓列表

```
GET /api/positions
Query Parameters:
  - status: 状态 (HOLDING/CLOSED)

Response:
{
  "code": 200,
  "data": {
    "total": 5,
    "totalMarketValue": 250000.00,
    "totalProfitLoss": 15000.00,
    "totalProfitRate": 0.06,
    "list": [
      {
        "id": 1,
        "tsCode": "000001.SZ",
        "stockName": "平安银行",
        "buyPrice": 12.00,
        "buyDate": "2026-02-01",
        "quantity": 1000,
        "currentPrice": 12.50,
        "marketValue": 12500.00,
        "profitLoss": 500.00,
        "profitRate": 0.0417,
        "holdingDays": 10,
        "status": "HOLDING"
      }
    ]
  }
}
```

#### 4.2.2 新增持仓

```
POST /api/positions
Request Body:
{
  "tsCode": "000001.SZ",
  "stockName": "平安银行",
  "buyPrice": 12.00,
  "buyDate": "2026-02-01",
  "quantity": 1000,
  "buySignalId": 123,
  "notes": "战略买入点"
}

Response:
{
  "code": 200,
  "message": "持仓添加成功",
  "data": {
    "id": 1
  }
}
```

#### 4.2.3 平仓

```
PUT /api/positions/{id}/close
Request Body:
{
  "sellPrice": 12.50,
  "sellDate": "2026-02-11",
  "sellSignalId": 456,
  "notes": "止盈"
}

Response:
{
  "code": 200,
  "message": "平仓成功"
}
```

### 4.3 回测管理 API

#### 4.3.1 创建回测任务

```
POST /api/backtests
Request Body:
{
  "backtestName": "趋势策略回测-2023",
  "strategyName": "trend_following",
  "startDate": "2023-01-01",
  "endDate": "2023-12-31",
  "initialCapital": 500000,
  "config": {
    "commission": 0.0003,
    "slippage": 0.001,
    "maxPositions": 5
  }
}

Response:
{
  "code": 200,
  "message": "回测任务创建成功",
  "data": {
    "id": 1,
    "status": "RUNNING"
  }
}
```

#### 4.3.2 获取回测结果

```
GET /api/backtests/{id}

Response:
{
  "code": 200,
  "data": {
    "id": 1,
    "backtestName": "趋势策略回测-2023",
    "strategyName": "trend_following",
    "startDate": "2023-01-01",
    "endDate": "2023-12-31",
    "initialCapital": 500000,
    "finalCapital": 575000,
    "totalReturn": 0.15,
    "annualReturn": 0.15,
    "maxDrawdown": 0.12,
    "sharpeRatio": 1.25,
    "winRate": 0.58,
    "profitLossRatio": 2.1,
    "totalTrades": 50,
    "winTrades": 29,
    "lossTrades": 21,
    "status": "COMPLETED",
    "resultDetail": {
      "equityCurve": [...],
      "trades": [...],
      "monthlyReturns": [...]
    }
  }
}
```

### 4.4 策略管理 API

#### 4.4.1 获取策略列表

```
GET /api/strategies

Response:
{
  "code": 200,
  "data": [
    {
      "id": 1,
      "strategyName": "trend_following",
      "strategyType": "TREND",
      "enabled": true,
      "weight": 0.6,
      "params": {
        "maPeriod": 250,
        "stopLoss": 0.08
      },
      "description": "趋势跟踪策略"
    }
  ]
}
```

#### 4.4.2 更新策略配置

```
PUT /api/strategies/{id}
Request Body:
{
  "enabled": true,
  "weight": 0.7,
  "params": {
    "maPeriod": 200,
    "stopLoss": 0.10
  }
}

Response:
{
  "code": 200,
  "message": "策略配置更新成功"
}
```

---

## 5. 前端页面设计

### 5.1 页面结构

```
├── 首页 (Dashboard)
│   ├── 今日信号概览
│   ├── 持仓概览
│   └── 市场状态
│
├── 信号看板 (Signals)
│   ├── 信号列表
│   ├── 信号详情
│   └── 信号筛选
│
├── 持仓管理 (Positions)
│   ├── 持仓列表
│   ├── 新增持仓
│   ├── 平仓操作
│   └── 持仓统计
│
├── 回测分析 (Backtest)
│   ├── 回测列表
│   ├── 创建回测
│   ├── 回测结果
│   └── 绩效分析
│
├── 策略管理 (Strategy)
│   ├── 策略列表
│   ├── 策略配置
│   └── 策略启用/禁用
│
└── 系统设置 (Settings)
    ├── 通知配置
    ├── 数据源配置
    └── 系统日志
```

### 5.2 核心页面设计

#### 5.2.1 首页 (Dashboard)

**布局**:
```
┌─────────────────────────────────────────────────┐
│  今日信号: 5个买入 | 2个卖出 | 市场状态: 牛市   │
├─────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐            │
│  │ 最新买入信号 │  │ 最新卖出信号 │            │
│  │ (实时更新)   │  │ (实时更新)   │            │
│  └──────────────┘  └──────────────┘            │
├─────────────────────────────────────────────────┤
│  ┌──────────────────────────────────────────┐  │
│  │ 持仓概览                                  │  │
│  │ 总市值: ¥250,000 | 盈亏: +¥15,000 (6%)  │  │
│  │ [持仓列表...]                            │  │
│  └──────────────────────────────────────────┘  │
└─────────────────────────────────────────────────┘
```

#### 5.2.2 信号看板

**功能**:
- 信号列表（表格）
- 筛选器（日期、类型、策略、周期）
- 信号详情（弹窗）
- K 线图展示（ECharts）

**表格列**:
- 时间
- 股票代码/名称
- 信号类型
- 价格
- 周期
- 策略
- 评分
- 状态
- 操作

#### 5.2.3 回测分析

**功能**:
- 回测参数配置
- 回测进度显示
- 结果可视化
  - 收益曲线
  - 回撤曲线
  - 月度收益热力图
  - 交易分布图

**关键指标**:
- 总收益率
- 年化收益率
- 最大回撤
- 夏普比率
- 胜率
- 盈亏比
- 交易次数

---

## 6. 回测引擎设计

### 6.1 回测流程

```
1. 加载历史数据
   ↓
2. 初始化账户
   - 初始资金
   - 手续费率
   - 滑点设置
   ↓
3. 按时间顺序遍历
   ↓
4. 每个时间点:
   - 更新持仓市值
   - 执行策略逻辑
   - 生成交易信号
   - 模拟订单执行
   - 记录交易
   ↓
5. 计算绩效指标
   ↓
6. 生成回测报告
```

### 6.2 核心类设计

```java
// 回测引擎
public class BacktestEngine {
    private BacktestConfig config;
    private Account account;
    private Strategy strategy;
    private List<Trade> trades;

    public BacktestResult run() {
        // 回测主循环
    }
}

// 账户
public class Account {
    private BigDecimal cash;
    private Map<String, Position> positions;
    private BigDecimal totalValue;

    public void buy(String tsCode, int quantity, BigDecimal price) {}
    public void sell(String tsCode, int quantity, BigDecimal price) {}
    public void updateMarketValue(Map<String, BigDecimal> prices) {}
}

// 交易记录
public class Trade {
    private String tsCode;
    private TradeType type; // BUY/SELL
    private LocalDateTime time;
    private BigDecimal price;
    private int quantity;
    private BigDecimal commission;
}

// 回测结果
public class BacktestResult {
    private BigDecimal totalReturn;
    private BigDecimal annualReturn;
    private BigDecimal maxDrawdown;
    private BigDecimal sharpeRatio;
    private Double winRate;
    private Double profitLossRatio;
    private List<EquityPoint> equityCurve;
    private List<Trade> trades;
}
```

---

## 7. 与 Python 引擎的集成

### 7.1 集成方式

**方案 A: REST API（推荐）**

```
Java 后端 ──HTTP──> Python 引擎
           <──JSON──
```

优点：
- 解耦，独立部署
- 语言无关
- 易于扩展

缺点：
- 网络开销
- 需要额外的 API 服务

**方案 B: 文件共享**

```
Python 引擎 ──写入──> 共享目录
Java 后端  ──读取──> 共享目录
```

优点：
- 简单直接
- 无需网络

缺点：
- 实时性差
- 文件锁问题

**方案 C: 消息队列**

```
Python 引擎 ──发布──> RabbitMQ/Kafka
Java 后端  ──订阅──> RabbitMQ/Kafka
```

优点：
- 异步解耦
- 高可靠性

缺点：
- 架构复杂
- 运维成本高

**推荐**: 第一版使用**文件共享**，后续升级到 **REST API**

### 7.2 数据同步

#### 7.2.1 信号同步

```
Python 引擎生成信号
    ↓
写入 output/signals_YYYYMMDD_HHMMSS.yaml
    ↓
Java 后端定时扫描
    ↓
解析 YAML 文件
    ↓
写入 MySQL 数据库
    ↓
前端实时展示
```

#### 7.2.2 持仓同步

```
用户在 Web 界面添加持仓
    ↓
Java 后端写入 MySQL
    ↓
同步到 data/positions.yaml
    ↓
Python 引擎读取
    ↓
监控卖出信号
```

---

## 8. 部署架构

### 8.1 开发环境

```
┌─────────────────┐
│  开发机          │
│  - Python 引擎  │
│  - Java 后端    │
│  - Vue 前端     │
│  - MySQL        │
│  - Redis        │
└─────────────────┘
```

### 8.2 生产环境

```
┌──────────────────────────────────────┐
│  服务器 (Linux)                       │
│  ┌────────────┐  ┌────────────┐     │
│  │ Python引擎 │  │ Java后端   │     │
│  │ (后台运行) │  │ (Jar包)    │     │
│  └────────────┘  └────────────┘     │
│  ┌────────────┐  ┌────────────┐     │
│  │ MySQL      │  │ Redis      │     │
│  └────────────┘  └────────────┘     │
│  ┌────────────────────────────┐     │
│  │ Nginx (静态文件 + 反向代理)│     │
│  └────────────────────────────┘     │
└──────────────────────────────────────┘
```

---

## 9. 开发计划

### 9.1 里程碑

| 阶段 | 时间 | 交付物 |
|------|------|--------|
| M1: 数据库设计 | 3 天 | 表结构、初始化脚本 |
| M2: Java 后端 API | 2 周 | 完整 REST API |
| M3: Vue 前端页面 | 3 周 | 所有页面和交互 |
| M4: 回测引擎 | 2 周 | 回测功能 |
| M5: 集成联调 | 1 周 | 前后端集成 |
| M6: 测试部署 | 1 周 | 测试和部署 |

**总计**: 约 10 周

---

## 10. 风险评估

| 风险 | 概率 | 影响 | 应对措施 |
|------|------|------|----------|
| Python-Java 集成复杂 | 中 | 高 | 先用文件共享，简化集成 |
| 回测性能问题 | 中 | 中 | 优化算法，使用缓存 |
| 前端开发周期长 | 高 | 中 | 分阶段交付，先核心功能 |
| 数据同步延迟 | 低 | 低 | 定时任务频率可调 |

---

## 11. 下一步

完成第二阶段后：
- 移动端 App
- 机器学习模型
- 实时监控告警
- 多账户管理

---

**文档状态**: ✅ 已完成
**审核状态**: 待审核
**下一步**: 编写详细设计文档
