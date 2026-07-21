from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """项目配置，从 .env 文件读取"""

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    model_name: str = "gpt-4o-mini"
    temperature: float = 0.7

    # ReAct 循环
    max_react_steps: int = 5

    # MCP 配置
    mcp_enabled: bool = False
    mcp_server_url: str = "http://127.0.0.1:9123/mcp"

    # RAG 配置（第5期）
    embedding_model: str = "text-embedding-3-small"
    kb_dir: str = "app/agent/rag/knowledge"
    # 向量后端：numpy（手写余弦，教学透明，零依赖，默认）/ chroma（向量数据库，生产代表，需 pip install chromadb）
    rag_backend: str = "numpy"
    # NumpyBackend 的 JSON 索引路径
    kb_index_path: str = "app/sessions/kb_index.json"
    # ChromaBackend 的持久化目录与 collection 名
    chroma_persist_dir: str = "app/sessions/chroma"
    chroma_collection: str = "ecom_kb"

    # Multi-Agent 配置（第6期）
    multi_agent_enabled: bool = False

    # Memory 配置（第7期）
    memory_enabled: bool = True
    memory_dir: str = "app/sessions/memory"
    memory_user_id: str = "default"
    max_ltm_facts: int = 50

    # Skill 配置（第8期）
    skills_enabled: bool = True
    skills_dir: str = "app/agent/skills/definitions"

    # Evaluation 配置（第9期，离线评估工具，无聊天开关）
    eval_dataset_path: str = "app/evaluation/cases.json"
    eval_use_judge: bool = True  # 是否启用 LLM-as-judge（质量/幻觉/过程合理性）
    eval_pass_threshold: float = 0.6  # 单维度通过阈值（judge 归一化到 0-1 后比较）
    eval_baseline_path: str = "app/evaluation/baseline.json"  # 回归基线
    eval_regression_tolerance: float = 0.05  # 单指标允许的最大回退幅度

    # API 服务（Web 流式对话）
    api_host: str = "127.0.0.1"
    api_port: int = 8010  # 默认 8010，避开常被占用的 8000

    # 真实数据层（W1.5）
    db_path: str = "app/sessions/ecom.db"

    # 可观测性（W2）
    obs_enabled: bool = True
    trace_db_path: str = "app/sessions/traces.db"
    price_per_1k_prompt: float = 0.0015      # 成本估算（美元/1k tokens，仅参考）
    price_per_1k_completion: float = 0.002

    # 安全护栏（W3）
    guardrails_enabled: bool = True

    # 人机协作 HITL（W3）
    hitl_enabled: bool = True
    hitl_confidence_threshold: float = 0.6
    hitl_db_path: str = "app/sessions/hitl.db"
    manual_mode_timeout: int = 3600   # 人工接管超时（秒），超时自动回落自动模式

    # 生产加固（W3.5）
    admin_token: str = ""              # 管理接口令牌；空=本地不鉴权
    rate_limit_per_min: int = 20       # 每会话每分钟最大请求数
    daily_request_budget: int = 500    # 每日全局请求上限（成本兜底）
    fast_path_enabled: bool = True     # 规则快路径（高频简单意图秒回）

    # 议价功能
    bargain_enabled: bool = True
    bargain_floor_ratio: float = 0.85   # 未设 floor_price 时：底价 = 标价 × 该系数
    bargain_max_rounds: int = 5         # 达到该轮次后直接让到底价
    bargain_decay: float = 0.5          # 阶梯让价衰减系数（越大让得越慢）

    # 多轮对话管理
    session_path: str = "app/sessions/session.json"
    history_threshold: int = 10  # 消息压缩策略通常为上下文达到一定的token数，例如claude code通常为达到最大上下文窗口的70%左右，此处简略为原始消息条数超过10轮
    history_keep_recent: int = 3  # 压缩时保留最近 3 条原始消息

    model_config = {"env_file": ".env"}


settings = Settings()
