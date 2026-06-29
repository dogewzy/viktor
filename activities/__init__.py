"""Temporal activities：包装现有副作用函数（DB 读写、K8s Job、GitLab、钉钉）。

约定：
- activity 在 worker 进程执行（非 workflow 沙箱），可自由做同步 IO。
- 现有服务函数多为同步（SQLAlchemy/requests），统一用 asyncio.to_thread 包成 async。
- 入参/返回值只用 JSON 可序列化类型（str/dict/list/bool/None），不传 ORM 对象。
"""
