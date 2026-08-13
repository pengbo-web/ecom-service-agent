import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

SESSION_VERSION = 1


def save_session(
    path: str,
    messages: list[dict],
    summary: Optional[str],
    short_term_memory: Optional[dict] = None,
    status: Optional[str] = None,
    step_seq: Optional[int] = None,
    pending: Optional[dict] = None,
) -> None:
    """把对话状态原子写入 JSON 文件。

    messages 只包含原始 user/assistant 条目（不含 system / summary）。
    short_term_memory 为短期记忆的序列化数据（第7期）。

    **`status` / `step_seq` / `pending` 是后补的,补的原因**(走查会话恢复时实测):
    这三个字段原来根本没被写进文件,而 `EcomAgent.__init__` 读的正是它们——

        self._status = loaded.get("status", "complete")   # 文件后端永远读到默认值
        if loaded.get("pending"): ...                     # 永远为空
        if self._status == "in_flight":
            self.raw_messages = sanitize_tool_pairs(...)  # 这条分支从不执行

    后果有三:①中断回合的 tool_calls 修复分支形同虚设;②注释里写的
    "R3:恢复挂起动作(确认前重启也能续)"在文件后端上不成立;③`_checkpoint()`
    每步落盘,存的东西里却没有 checkpoint 需要的字段——纯付 I/O。

    而 Redis 后端(`RedisSessionStore`)是整块 JSON 存取,这三个字段一直是保真的。
    于是同一份代码在两个后端上行为不同,**并且 Redis 连不上时会降级到文件后端**
    ——偏偏那正是最可能发生重启的时刻。
    """
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "version": SESSION_VERSION,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summary,
        "messages": messages,
        "short_term_memory": short_term_memory,
        "status": status,
        "step_seq": step_seq,
        "pending": pending,
    }

    tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, file_path)


def load_session(path: str) -> Optional[dict]:
    """读取会话文件。不存在或损坏都返回 None（降级为新会话）。"""
    file_path = Path(path)
    if not file_path.exists():
        return None

    try:
        with file_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"⚠️  会话文件损坏，已忽略（{e}）")
        return None

    if not isinstance(data, dict) or "messages" not in data:
        print("⚠️  会话文件格式不识别，已忽略")
        return None

    # 老会话文件里没有 status/step_seq/pending 这三个键(它们是后补的,见 save_session
    # 的说明)。这里**不替它们编默认值**——原样返回 None,由调用方
    # (`EcomAgent.__init__` 的 `loaded.get("status", "complete")`)按它自己的语义兜底。
    # 在这里塞一个 "complete" 会让"老文件没这个字段"和"这会话确实是完成态"变得分不开。
    return {
        "summary": data.get("summary"),
        "messages": data.get("messages", []),
        "short_term_memory": data.get("short_term_memory"),
        "status": data.get("status"),
        "step_seq": data.get("step_seq"),
        "pending": data.get("pending"),
    }


def delete_session(path: str) -> None:
    """删除会话文件，不存在时静默。"""
    file_path = Path(path)
    if file_path.exists():
        file_path.unlink()
