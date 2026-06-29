"""Temporal workflows：issue-intake → coding-task 的 durable 编排大脑。

workflow 代码必须确定性：禁直接 DB/IO/datetime.now，一切副作用走 activity，
时间用 workflow.now()。第三方导入用 workflow.unsafe.imports_passed_through()。
"""
