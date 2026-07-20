from app.db.database import Database

_DB: Database | None = None


def get_db() -> Database:
    global _DB
    if _DB is None:
        _DB = Database()
    return _DB


def set_db(db: Database) -> None:
    global _DB
    _DB = db


__all__ = ["Database", "get_db", "set_db"]
