"""Temporal 编排层：issue-intake → coding-task 的 durable workflow。

Temporal = 编排大脑 + 唯一写者；现有 DB 表 = 读模型投影（前端继续轮询，零改动）。
重活（LLM agent loop）仍跑在现有 K8s Job 里，workflow 只派发并等待。
"""
