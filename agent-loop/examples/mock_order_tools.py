"""业务路由教学使用的模拟订单工具，不连接真实数据库。"""

from __future__ import annotations

from pi_agent_loop import AgentTool, AgentToolResult

_MOCK_ORDERS = {
    "1001": "已发货",
    "1002": "待支付",
    "1003": "已完成",
}


def _validate(arguments):
    if not isinstance(arguments, dict):
        raise ValueError("参数必须是对象")
    order_id = arguments.get("order_id")
    if not isinstance(order_id, str) or not order_id.strip():
        raise ValueError("order_id 必须是非空字符串")
    return {"order_id": order_id.strip()}


async def _execute(_call_id, arguments, cancellation, _on_update):
    cancellation.throw_if_cancelled()
    order_id = arguments["order_id"]
    status = _MOCK_ORDERS.get(order_id, "订单不存在")
    return AgentToolResult(
        content=[
            {
                "type": "text",
                "text": f"订单 {order_id} 当前状态：{status}",
            }
        ],
        details={
            "source": "mock_order_database",
            "orderId": order_id,
            "status": status,
        },
    )


def create_mock_order_status_tool() -> AgentTool:
    return AgentTool(
        name="get_order_status",
        label="查询订单状态",
        description="从订单业务系统读取指定订单号的当前真实状态",
        parameters={
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "需要查询的订单号",
                }
            },
            "required": ["order_id"],
            "additionalProperties": False,
        },
        validate_args=_validate,
        execute=_execute,
        timeout_seconds=5,
    )
