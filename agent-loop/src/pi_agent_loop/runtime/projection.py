"""把内部 RunState 转换成 UI/日志可读视图。"""

from __future__ import annotations

from typing import Any

from .states import RunState

_PHASE_LABELS = {
    "idle": "空闲",
    "running": "运行中",
    "routing": "正在理解和路由请求",
    "requesting_model": "正在请求模型",
    "executing_tools": "正在执行工具",
    "retrying": "正在等待模型重试",
    "compacting": "正在压缩上下文",
    "waiting_approval": "等待审批",
    "outcome_unknown": "操作结果待核对",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "suspended": "已挂起，等待恢复",
}


def project_runtime_state(state: RunState) -> dict[str, Any]:
    tools = [
        {
            "toolCallId": tool.tool_call_id,
            "toolName": tool.tool_name,
            "phase": tool.phase,
            "retryAttempt": tool.retry_attempt,
            "errorCode": tool.error_code,
        }
        for tool in state.tools.values()
    ]
    return {
        "runId": state.run_id,
        "phase": state.phase,
        "phaseLabel": _PHASE_LABELS[state.phase],
        "terminal": state.terminal,
        "turn": state.turn,
        "modelRetryAttempt": state.model_retry_attempt,
        "activeToolCount": state.active_tool_count,
        "tools": tools,
        "failureCode": state.failure_code,
        "routingStatus": state.routing_status,
        "lastEvent": state.last_event,
        "sequence": state.sequence,
    }
