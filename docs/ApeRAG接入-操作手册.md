# ApeRAG 接入操作手册(一次性初始化)

部署形态:全定义/选择性启用——镜像全在本地,默认只跑核心 8 容器。

## 服务地址
- Web 界面: http://127.0.0.1:3100/
- API 文档: http://127.0.0.1:8100/docs

## 按需启用/关闭重量级组件
    cd D:\2026项目\ApeRAG
    docker compose --profile neo4j up -d       # 开 GraphRAG(演示前建议先 stop Langfuse 栈腾内存)
    docker compose --profile neo4j stop neo4j  # 用完关
    docker compose --profile monitoring up -d  # 开 flower(celery 监控)
    docker compose --profile docray up -d      # 开重型文档解析(8G 预留,md 语料用不到)

## 一次性初始化(用户在浏览器操作)
1. 打开 http://127.0.0.1:3100/ ,注册账号(首个账号即管理员)并登录
2. 模型提供商:设置 → 模型服务商 → 添加 OpenAI 兼容提供商,
   Base URL 与 API Key 填 ecom 项目 .env 里的 OPENAI_BASE_URL / OPENAI_API_KEY;
   确认 embedding 模型(text-embedding-3-small 或提供商等价物)与一个对话模型可用,
   并在"默认模型"里把 embedding 默认项设置好
3. 新建知识库(collection):名称「并夕夕客服知识库」,
   索引开关:向量 ✅ 全文 ✅ 知识图谱 ❌(neo4j 未启动) 摘要 ❌ 视觉 ❌
4. 上传全部 13 篇文档:D:\2026项目\ecom-service-agent\app\agent\rag\knowledge\ 下的
   退换货政策 / 配送说明 / 会员权益 / 常见问题FAQ / 优惠券与促销规则 / 价格保护政策 /
   发票与支付说明 / 售后维修与三包 / 物流异常与赔付标准 / 订单管理规则 /
   投诉与纠纷处理 / 特殊品类服务规则 / 账户与安全(共 13 个 .md),等待索引状态全部完成
5. 生成 API Key:设置 → API Keys → 创建,复制
6. 取 collection id:知识库详情页 URL 中 col_ 开头的段
7. 把以下四行追加到 ecom 项目 .env(值换成实际):
       KB_BACKEND=aperag
       APERAG_BASE_URL=http://127.0.0.1:8100
       APERAG_API_KEY=sk-xxxx
       APERAG_COLLECTION_ID=col_xxxx

## 验收
    cd D:\2026项目\ecom-service-agent
    .venv\Scripts\python.exe scripts\aperag_smoke.py "七天无理由退货怎么退"
预期:HTTP 200,命中若干条,来源含「退换货政策」,recall_type 含 vector_search 或 fulltext_search。

## 回滚
.env 里 KB_BACKEND=local 即回到项目内本地索引(或删除该行,默认就是 local)。
