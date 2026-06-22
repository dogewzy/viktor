"""Viktor 自身数据库引擎（存储注册项持久化数据）。"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from settings import database_config

engine = create_engine(
    database_config.url,
    pool_size=5,
    max_overflow=3,
    pool_recycle=1800,
    pool_pre_ping=True,
    echo=False,
)

SessionLocal = sessionmaker(bind=engine)


def get_db() -> Session:
    """FastAPI 依赖注入用的 session 工厂。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
