from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """项目配置，从 .env 文件读取"""

    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    model_name: str = "gpt-4o-mini"
    temperature: float = 0.2   # 客服要确定性,低温降幻觉/发散(生产建议 0.1-0.3)

    # 模型容错（Phase 1：fallback + 熔断 + 错误分类重试）
    resilience_enabled: bool = True
    llm_timeout_s: float = 120.0          # 单次 LLM 调用超时
    llm_max_retries: int = 2              # 主模型瞬时错误重试次数
    llm_retry_after_cap_s: float = 30.0   # Retry-After 的上限
    breaker_threshold: int = 3            # 主模型连续失败多少次跳闸
    breaker_cooldown_s: float = 60.0      # 熔断冷却秒数
    fallback_base_url: str = ""           # 备用模型端点（留空=无备用，仅重试）
    fallback_model: str = ""              # 备用模型 id（留空=无备用）
    fallback_api_key: str = ""            # 备用模型 key（留空=复用主 key）

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
    # 【已废弃】H1.0-C 起总控 Agent(MultiAgentOrchestrator)为唯一入口,恒当 True。
    # 字段保留仅为兼容既有 .env,不再影响运行时选择(工厂不再读它)。
    multi_agent_enabled: bool = False

    # 回复流水线（Phase H1：出话草稿 → 评估 → 重写 → 润色，可关）
    reply_pipeline_enabled: bool = True     # 总开关；关闭则原样返回草稿，不调用流水线内任何 LLM
    reply_pipeline_max_rounds: int = 2      # 评估-重写最多轮次；达到后强制 polish/done 收敛
    selector_mode: str = "llm"              # 总控选择下一步的方式：llm(默认，LLM 推理动态调度)/ rule(规则兜底)

    # Memory 配置（第7期）
    memory_enabled: bool = True
    memory_dir: str = "app/sessions/memory"
    memory_user_id: str = "default"
    max_ltm_facts: int = 50
    memory_curation_enabled: bool = False   # Phase 5:长期记忆 LLM 策展(合并/纠正/按重要性淘汰);关则用 add_facts

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

    # 空闲会话自动巩固长期记忆（Web 无"会话结束"信号，用空闲超时近似真实客服）
    auto_consolidate_enabled: bool = True
    session_idle_ttl: int = 1800      # 会话空闲多久（秒）后自动巩固记忆并从内存回收
    reaper_interval: int = 120        # 后台扫描间隔（秒）

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

    # 会话存储介质(R1):file=本地文件(默认,单机);redis=热会话共享+TTL(生产/多实例)
    session_store_backend: str = "file"
    redis_url: str = "redis://localhost:6379/0"
    session_ttl: int = 3600   # redis 热会话过期(秒),每次访问续期
    checkpoint_enabled: bool = True   # R2 步级 checkpoint:每工具步落盘,回合中途崩溃可恢复
    session_lock_ms: int = 30000      # R4 分布式会话锁超时(毫秒),防持有者崩溃后死锁
    archive_enabled: bool = True      # R5 会话结束/回收时冷归档到 SQLite(审计/离线分析)
    idempotency_enabled: bool = True  # R6 写工具幂等(仅 redis 后端生效),防重复副作用
    idempotency_ttl: int = 600        # 幂等键保留时长(秒)
    history_threshold: int = 10  # (兼容保留)条数触发阈值,现主用 token 预算
    history_keep_recent: int = 3  # 压缩时保留最近 3 条原始消息

    # 上下文防线（Phase 2，借鉴 nanobot context_governance）
    context_window_tokens: int = 30000    # 模型上下文窗口(保守默认,按实际模型调大)
    max_output_tokens: int = 2048         # 单次输出预留
    context_safety_buffer: int = 1024     # 安全缓冲
    tool_result_max_chars: int = 4000     # 单条工具结果超此字符数则落盘留指针(不丢信息)
    tool_result_preview_chars: int = 1500 # 落盘时在历史里保留的预览长度
    tool_result_dir: str = "app/sessions/tool_results"  # 超大工具结果存档目录

    model_config = {"env_file": ".env"}


settings = Settings()
