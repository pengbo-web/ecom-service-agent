from pydantic import BaseModel


class ChatRequest(BaseModel):
    session_id: str
    message: str
    confirm: bool = False   # 本轮是否授权执行风险动作(退款/成交),前端确认按钮置 true


class ResetRequest(BaseModel):
    session_id: str
