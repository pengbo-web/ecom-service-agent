r"""一次性迁移:把每个用户的碎片会话按时间合并进其规范会话,修复历史碎片化。

运行:.venv\Scripts\python.exe scripts\merge_conversations.py
之后需重启 agent(使内存态从合并后的存储重载)。单一连续会话已防新碎片,此脚本只清历史遗留。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

from app.db import get_db  # noqa: E402
from app.api.conversations import merge_user_conversations  # noqa: E402

db = get_db()
conn = db.connect()
users = [r[0] for r in conn.execute(
    "SELECT user_id FROM conversations WHERE status='open' "
    "GROUP BY user_id HAVING COUNT(*) > 1").fetchall()]
conn.close()

print(f"需合并的用户(多条 open 会话): {len(users)}")
for uid in users:
    r = merge_user_conversations(db, uid)
    if r:
        print(f"  用户 {uid}: 合并 {r['merged_msgs']} 条消息 → {r['canonical']}, 关闭碎片 {r['closed']}")
print("完成。请重启 agent。")
