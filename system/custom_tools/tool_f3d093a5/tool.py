# -*- coding: utf-8 -*-
"""自动生成的通用调用集: tool_f3d093a5"""
import json
from agent.tools.registry import ToolResult

async def handler(args, ctx):
    # 通用兜底: 把传入参数规整后返回, 供 Agent 判断是否满足需求
    cleaned = {k: v for k, v in args.items() if v is not None}
    if not cleaned:
        return ToolResult(ok=False, error="缺少参数", error_type="invalid_args")
    return ToolResult(data={"received": cleaned, "note": "通用调用集, 如需精确计算请说明参数含义"})
