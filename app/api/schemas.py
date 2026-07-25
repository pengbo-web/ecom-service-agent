from pydantic import BaseModel


class ChatRequest(BaseModel):
    session_id: str
    message: str
    confirm: bool = False   # 本轮是否授权执行风险动作(退款/成交),前端确认按钮置 true
    user_id: str = "default"   # 用户身份:长期记忆按 user_id 隔离(一人一档)


class ResetRequest(BaseModel):
    session_id: str
    user_id: str = "default"   # 重置后立刻服务端新开一个会话,归属该用户


class OpenConversationRequest(BaseModel):
    user_id: str = "default"
