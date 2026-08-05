from pydantic import BaseModel


class ChatRequest(BaseModel):
    session_id: str
    message: str
    confirm: bool = False   # 本轮是否授权执行风险动作(退款/成交),前端确认按钮置 true
    user_id: str = "default"   # 用户身份:长期记忆按 user_id 隔离(一人一档)
    hmdp_token: str = ""    # 接 hmdp 数据源时,前端传来的 hmdp 登录 token(解出 hmdp userId + 透传给 MCP 调 hmdp)
    current_item_id: str = ""   # 当前咨询商品(hmdp 商品 id):前端从 ?item= 或商品卡带入,用于"这是什么"等指代


class ResetRequest(BaseModel):
    session_id: str
    user_id: str = "default"   # 重置后立刻服务端新开一个会话,归属该用户


class OpenConversationRequest(BaseModel):
    user_id: str = "default"


class CreateUserRequest(BaseModel):
    user_id: str
    name: str = ""


class LoginRequest(BaseModel):
    user_id: str


class AgentReplyRequest(BaseModel):
    text: str = ""


class CreateOrderRequest(BaseModel):
    item_id: str            # hmdp 商品 id(自助下单:用户在商城/商品卡点『立即购买』)
    quantity: int = 1
    shipping_address: str = ""


class SkillDistillRequest(BaseModel):
    doc_text: str           # 产品资料/客服 SOP 正文(纯文本或 markdown)


class SellerChatRequest(BaseModel):
    session_id: str
    message: str


class ShopProfileRequest(BaseModel):
    """店主提交的店铺人格设定(店铺名/语气/禁语)。空 tone = 恢复默认语气。"""
    shop_name: str = ""
    tone: str = ""
    banned_words: str = ""


class ReviewRequest(BaseModel):
    """买家提交评价。user_id 由服务端 _resolve_user 解析,不信请求体自报的身份。"""
    order_id: str
    sku: str
    rating: int
    content: str = ""


class CartAddRequest(BaseModel):
    """加入购物车。item_id 与 CreateOrderRequest 同一命名空间(hmdp 商品 id/
    本地 sku),映射为 carts.sku;user_id 同样由服务端 _resolve_user 解析。"""
    item_id: str
    quantity: int = 1
