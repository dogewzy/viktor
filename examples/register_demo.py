"""
Viktor 注册示例 — 虚构的电商订单系统。

演示如何将业务接入 Viktor（不写死 SQL 模板）：
1. 注册项目
2. 注册业务上下文（流程、状态含义、常见排查）
3. 注册数据库连接器（只读）
4. （可选）注册术语表，帮助模型映射中文业务词 ↔ 库表/字段线索

使用方式：
  1. 启动 Viktor: python main.py
  2. 运行此脚本: python examples/register_demo.py
  3. 在钉钉群里 @机器人 提问；模型会 list/describe 后再自拟 SELECT。
"""
import httpx

VIKTOR_URL = "http://localhost:8080"
PROJECT_ID = "order-service"


def main() -> None:
    """注册虚构订单系统的最小接入集合（无 SQL 模板）。"""

    httpx.post(
        f"{VIKTOR_URL}/api/v1/register/project",
        json={
            "id": PROJECT_ID,
            "name": "订单系统",
            "description": "管理订单的创建、支付与履约（演示项目）",
        },
    )
    print("项目注册完成")

    httpx.post(
        f"{VIKTOR_URL}/api/v1/register/context",
        json={
            "id": "ecommerce-context",
            "project_id": PROJECT_ID,
            "priority": 1,
            "content": """
## 电商订单系统

### 核心表（示意）
- orders：订单主表，字段含 id, user_id, status, total_amount, created_at …
- order_status_history：状态流转历史

### 订单状态
- pending: 待支付
- paid: 已支付
- shipped: 已发货
- delivered: 已签收
- cancelled: 已取消
- refunded: 已退款

### 常见问题
- 订单长时间 pending：检查支付回调
- paid 未 shipped：检查发货队列
""",
        },
    )
    print("上下文注册完成")

    httpx.post(
        f"{VIKTOR_URL}/api/v1/register/database-connector",
        json={
            "id": "order-db",
            "project_id": PROJECT_ID,
            "type": "mysql",
            "host": "localhost",
            "port": 3306,
            "username": "readonly",
            "password": "readonly_pass",
            "database": "ecommerce",
            "readonly": True,
        },
    )
    print("数据库连接器注册完成")

    resp = httpx.get(f"{VIKTOR_URL}/api/v1/register/status")
    print(f"注册状态: {resp.json()}")


if __name__ == "__main__":
    main()
