"""一键建库 + 灌种子：python -m app.scripts.init_db"""

from pathlib import Path

from app.config.settings import settings
from app.db import Database
from app.db.seed import seed_from_mock


def main():
    db_file = Path(settings.db_path)
    if db_file.exists():
        db_file.unlink()
        print(f"已删除旧库: {db_file}")
    db = Database(settings.db_path)
    db.init_schema()
    seed_from_mock(db)
    print(f"建库完成并已灌入种子数据: {db_file}")


if __name__ == "__main__":
    main()
