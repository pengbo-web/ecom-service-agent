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
    # 开启前需:①MCP server 常驻(mcp_server/server.py 或 compose 的 mcp-server)②身份透传已实现(P1)
    # ——否则订单类工具在 server 进程拿不到用户上下文;连不上会自动降级本地工具(不崩)
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
    memory_fts_enabled: bool = True         # H2 FTS 记忆全文索引召回;关闭回退全量注入
    memory_profile_enabled: bool = True     # H2/G2 结构化用户档案(base/标签/工单)注入与落库;关闭即完全禁用
    stm_update_every_n_turns: int = 3     # 短期记忆每 N 轮更新一次(1=每轮,旧行为);省 token 且中间轮原始消息本就在上下文
    memory_checkpoint_every_n_turns: int = 10   # 长会话中途每 N 轮触发隐式记忆抽取归档(0=关);与会话末巩固双通道,策展去重
    memory_async_updates: bool = True     # 记忆更新走后台线程,不阻塞回复(False=同步,测试/调试)
    memory_bg_timeout_s: float = 60.0     # 后台高频记忆调用(STM/中途抽取)超时上限,零重试快速失败——防容错层重试累积卡死后台线程(实测曾出现单次 906s)

    # Skill 配置（第8期）
    skills_enabled: bool = True
    skills_dir: str = "app/agent/skills/definitions"
    # H3 Skill 离线合成入口开关（默认关，仅离线手动跑；产出候选，人工审核后才移入 definitions/ 生效）
    skill_synth_enabled: bool = False

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
    hitl_repeat_times: int = 3        # 同一问题重复 N 次未解决→自动转人工(文档9.④,difflib相似度判同)

    # FAQ 语义缓存(文档2.5缓存预热):高频问答预热直答,命中零LLM;种子来自常见问题FAQ.md
    faq_cache_enabled: bool = True
    faq_cache_path: str = "app/sessions/faq_cache.json"
    faq_cache_min_score: float = 0.90   # 余弦阈值:高置信才直答,答错比答慢更伤信任

    # 意图过滤检索(文档2.2三级索引):检索行按 QU domain 排序/过滤
    # off=不动 | boost(默认)=匹配域稳定前置,不丢行 | strict=只留匹配域,空则回退全量
    recall_domain_mode: str = "boost"

    # 空闲会话自动巩固长期记忆（Web 无"会话结束"信号，用空闲超时近似真实客服）
    auto_consolidate_enabled: bool = True
    session_idle_ttl: int = 1800      # 会话空闲多久（秒）后自动巩固记忆并从内存回收
    reaper_interval: int = 120        # 后台扫描间隔（秒）
    # 空闲回收时是否把会话置 closed(工单式,下次打开翻篇开新会话)。
    # False(默认)=不自动结束会话:仍巩固记忆+回收内存,但会话保持 open,
    # 下次打开由 open_or_reuse 复用原会话(持久会话式,更贴 Web 聊天习惯)。
    conversation_idle_close_enabled: bool = False

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
    session_snapshot_enabled: bool = True   # 每回合冷快照(SQLite upsert),热会话过期后历史回显兜底;关=回退现状
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

    # Langfuse 观测平台接入(可选体验层,自托管 localhost:3000)
    langfuse_enabled: bool = False     # 开=LLM 调用自动上报 Langfuse(需 pip install langfuse)
    langfuse_public_key: str = ""      # 在 Langfuse UI 建项目后生成(pk-lf-...)
    langfuse_secret_key: str = ""      # (sk-lf-...);均放 .env,勿提交
    langfuse_host: str = "http://localhost:3000"

    # H1 接地上下文(评估/重写用的本轮工具真实结果)截断预算
    # 教训:500 字会把常见结果拦腰切断,评估器误判草稿"编造"、重写反把正确回复改坏
    grounding_result_max_chars: int = 2000   # 单条工具结果上限
    grounding_total_max_chars: int = 8000    # 本轮全部工具结果总预算(优先保最近)

    # 统一召回层(Unified Recall):存储分离、召回统一——每轮预检索注入,
    # 政策类知识不再依赖模型自觉调 search_knowledge(实测触发率低,是编造空档)
    recall_kb_enabled: bool = True        # KB 预召回开关;关=回到纯工具式
    recall_kb_top_k: int = 2              # 每轮最多注入的 KB 片段数
    recall_kb_min_score: float = 0.30     # 相似度阈值,低于不注入(防无关知识污染上下文)
    recall_kb_max_chars: int = 1200       # 注入段字符预算,超出丢弃后续片段
    recall_kb_min_query_chars: int = 4    # 问题太短(如"嗯")不触发,省一次 embedding
    recall_kb_timeout_s: float = 6.0      # 检索 embedding 超时(热路径,每轮必经):快速失败,挂起不能拖垮回复
    recall_kb_embed_retries: int = 0      # 热路径零重试(重试累积曾致后台单次906s,同教训)

    # ApeRAG 外部 RAG 接入(kb_backend=aperag 时生效;三级降级 aperag→local→无注入)
    kb_backend: str = "local"          # local=项目内向量索引; aperag=外部 ApeRAG(向量+全文混合)
    aperag_base_url: str = "http://127.0.0.1:8100"
    aperag_api_key: str = ""           # ApeRAG 控制台创建(Bearer);放 .env 勿提交
    aperag_collection_id: str = ""     # 知识库 collection id(col_ 开头)
    aperag_rerank: bool = False        # 预召回热路径默认关重排(省延迟);深查精度可开
    # 向量路相似度阈值。0.2=ApeRAG 服务端 Field 默认值(其 Web 搜索页用 0.7,精确率优先);
    # 预召回选低阈值走召回率优先,下游有字符预算+评估器兜底。必须显式传:ApeRAG API 层
    # 会把缺省字段解析成 None 显式下传,覆盖 Field 默认导致整体 500(上游 bug,可提 issue)
    aperag_min_similarity: float = 0.2
    # aperag 故障时是否降级本地索引。False(默认)=纯 ApeRAG 体验,故障=本轮无KB注入(可感知);
    # True=三级降级 aperag→local→无注入(生产建议开,故障静默兜底)
    kb_local_fallback_enabled: bool = False
    # ApeRAG 检索超时。实测暖机 0.9-1.1s,容器冷启首查 3.5s+;6s(embedding 快速失败值)会误杀
    # 冷启首查,单独放宽到 10s——仍有界防挂死,超时行为=该轮无注入(或降级,看上面开关)
    aperag_timeout_s: float = 10.0

    # 统一查询理解节点(意图识别):一次 LLM 调用出 domain/intent/need_kb/kb_query,
    # 吃掉独立路由与改写调用;闲聊轮免检索。关=回退老 Router 路由+每轮必检索
    # (改写能力已并入本节点,回退路径检索用原句)
    query_understanding_enabled: bool = True

    # 极简登录态(两档用户体系:先创建才可用 + 身份从签名 token 解出)
    auth_enabled: bool = True          # 关=完全回退自报 user_id(测试/教学)
    auth_secret: str = "dev-secret-change-in-prod"   # 生产必须换(env AUTH_SECRET)
    auth_token_ttl: int = 86400        # token 有效期(秒)

    model_config = {"env_file": ".env"}


settings = Settings()
