from app.db.database import Database

_DB: Database | None = None


def get_db() -> Database:
    global _DB
    if _DB is None:
        _DB = Database()
        # 首次构造即建表(纯 CREATE TABLE IF NOT EXISTS,幂等无损):
        # 新环境/旧库缺新表(如 conversations)时,核心链路不会因缺表 500。
        try:
            _DB.init_schema()
        except Exception:  # noqa: BLE001 建表失败不阻断(只读库等场景),按原行为暴露于首次使用
            pass
    return _DB


def set_db(db: Database) -> None:
    global _DB
    _DB = db


__all__ = ["Database", "get_db", "set_db"]
