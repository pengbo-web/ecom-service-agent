# hmdp 电商化 + AI 客服接入 实施方案

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:subagent-driven-development 或 superpowers:executing-plans 逐任务执行。步骤用 `- [ ]` 复选框跟踪。

**Goal:** 给黑马点评(hmdp)新增一套实物电商模块(商品/订单/物流/退款),写一个 hmdp-mcp 适配层把它包成 AI 客服 agent 认识的工具,并加 H5 商城页,最终实现"逛店→下单→问客服(查单/查物流/退款/议价)"的真实闭环。

**Architecture:** hmdp(Java :8085)新增电商表与 REST 接口 → hmdp-mcp(Python FastMCP :9123)调 hmdp REST 并把字段映射成 agent 的数据契约、透传身份 → ecom-service-agent(Python :8010)核心不改,`MCP_ENABLED=true` 时工具走 hmdp-mcp。身份靠共享 Redis(`login:token:{token}` Hash 的 id 字段)或 hmdp `/user/me` 打通。前端 H5(nginx :8080)加商城页,复用现成 axios+token 封装。

**Tech Stack:** hmdp = SpringBoot 2.3.12 + MyBatis-Plus 3.4.3 + MySQL 5.7 + Redis;hmdp-mcp = Python + FastMCP(mcp>=1.8)+ httpx;agent = 现有 Python 栈;前端 = Vue2 + Element UI(多页,无构建)。

## Global Constraints

- hmdp 后端根目录:`D:\BaiduNetdiskDownload\02-实战篇\代码\hm-dianping`,包名 `com.hmdp`,端口 **8085**,JDK 8 运行。
- hmdp 前端根目录:`C:\Users\pb166\hmdp-nginx\nginx-1.18.0\html\hmdp`,nginx 端口 **8080**,`/api` 反代到 8085。
- agent 根目录:`D:\2026项目\ecom-service-agent`,端口 **8010**,`MCP_SERVER_URL=http://127.0.0.1:9123/mcp`。
- MySQL:库 `heimadp`,`root / Root@123456`,3306。Redis:`127.0.0.1:6379` 无密码。
- **只新增,不改 hmdp 原有点评/券/秒杀业务与表**。新表前缀 `tb_` 与原表并存。
- **金额**:hmdp 电商表金额一律存**分(Long)**(与 hmdp 现有 payValue 一致);hmdp-mcp 层统一 `/100` 转成**元**给 agent。
- **订单状态**:hmdp `tb_order.status` 直接用 agent 的英文枚举 `pending/shipped/delivered/refund_processing/cancelled`(避免枚举映射)。
- **身份命名空间**:agent 的 `user_id` == hmdp 的 `userId(字符串)` == `tb_order.user_id` == MCP 透传的 `ctx_user_id`,全程同一套。
- MCP 工具返回值必须是 `json.dumps(dict, ensure_ascii=False)` 字符串;订单/用户相关工具签名带 `ctx_user_id: str = ""` 且首行 `set_current_user(ctx_user_id or None)`。
- 每个 Java 改动后用 IntelliJ 重启 hmdp 验证;Python 部分用 pytest(agent 的 `.venv`);前端用浏览器验证。

---

## File Structure

**Phase 1 — hmdp 后端电商模块**(`hm-dianping/src/main/`)
- Create: `resources/db/ecommerce.sql` — 新增电商表 + 种子数据(建表脚本,单独文件,不动 hmdp.sql)
- Create: `java/com/hmdp/entity/Product.java` `Order.java` `OrderItem.java` `Shipment.java` `LogisticsEvent.java`
- Create: `java/com/hmdp/mapper/ProductMapper.java` `OrderMapper.java` `OrderItemMapper.java` `ShipmentMapper.java` `LogisticsEventMapper.java`(空继承壳)
- Create: `java/com/hmdp/service/IProductService.java` `IOrderService.java` + `service/impl/ProductServiceImpl.java` `OrderServiceImpl.java`
- Create: `java/com/hmdp/controller/ProductController.java` `OrderController.java` `LogisticsController.java`
- Create: `java/com/hmdp/dto/PlaceOrderDTO.java`(下单入参)
- Modify: `java/com/hmdp/config/MvcConfig.java`(放行 `/product/**`、`/logistics/**` 供游客浏览)

**Phase 2 — hmdp-mcp 适配层**(`ecom-service-agent/`)
- Create: `mcp_server/hmdp_client.py` — hmdp REST 的 httpx 封装 + token 透传
- Create: `mcp_server/hmdp_mapping.py` — hmdp DTO ↔ agent 契约 的纯函数映射(可单测)
- Create: `mcp_server/hmdp_server.py` — FastMCP server(:9123),@mcp.tool 包装全部工具
- Create: `tests/test_hmdp_mapping.py`(映射纯函数单测)
- Create: `tests/test_hmdp_server_tools.py`(MCP 工具用 mock hmdp 的集成测)

**Phase 3 — 打通验证**(`ecom-service-agent/`)
- Modify: `app/agent/tools/validation.py`(订单号正则参数化/放宽)
- Modify: `.env`(`MCP_ENABLED=true`;身份对齐说明)
- Create: `app/api/hmdp_identity.py` — 会话用户 ↔ hmdp userId 解析(读共享 Redis 或调 /user/me)
- Create: `tests/test_hmdp_identity.py`

**Phase 4 — 前端商城页**(`hmdp-nginx/nginx-1.18.0/html/hmdp/`)
- Create: `product-list.html` `product-detail.html` `my-order.html`
- Modify: `js/footer.js`(底部导航加"商城"入口)

---

# Phase 1 — hmdp 后端电商模块

## Task 1.1: 建电商表 + 种子数据

**Files:**
- Create: `hm-dianping/src/main/resources/db/ecommerce.sql`

**Interfaces:**
- Produces: 表 `tb_product / tb_order / tb_order_item / tb_shipment / tb_logistics_event`,列名/语义与 agent 的 SQLite schema 对齐(便于 MCP 近 1:1 映射)。

- [ ] **Step 1: 写建表+种子 SQL**

`ecommerce.sql`:
```sql
SET NAMES utf8mb4;

DROP TABLE IF EXISTS `tb_product`;
CREATE TABLE `tb_product` (
  `id` bigint UNSIGNED NOT NULL AUTO_INCREMENT,
  `title` varchar(128) NOT NULL COMMENT '商品名',
  `category` varchar(32) DEFAULT NULL COMMENT '类目',
  `images` varchar(1024) DEFAULT NULL COMMENT '图片,逗号分隔',
  `price` bigint NOT NULL COMMENT '售价(分)',
  `floor_price` bigint DEFAULT NULL COMMENT '议价底价(分),不可对外',
  `stock` int NOT NULL DEFAULT 0,
  `sku` varchar(64) DEFAULT NULL,
  `description` varchar(1024) DEFAULT NULL,
  `specs` varchar(1024) DEFAULT NULL COMMENT '规格 JSON',
  `status` int NOT NULL DEFAULT 1 COMMENT '1上架 2下架',
  `create_time` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

DROP TABLE IF EXISTS `tb_order`;
CREATE TABLE `tb_order` (
  `id` bigint UNSIGNED NOT NULL AUTO_INCREMENT,
  `order_no` varchar(32) NOT NULL COMMENT '对外订单号 ORD-yyyyMMdd-序号',
  `user_id` bigint NOT NULL COMMENT '归属用户(hmdp userId)',
  `status` varchar(24) NOT NULL DEFAULT 'pending' COMMENT 'pending/shipped/delivered/refund_processing/cancelled',
  `total` bigint NOT NULL COMMENT '总额(分)',
  `shipping_address` varchar(255) DEFAULT NULL,
  `tracking_number` varchar(64) DEFAULT NULL,
  `carrier` varchar(32) DEFAULT NULL,
  `refund_reason` varchar(255) DEFAULT NULL,
  `refund_status` varchar(24) DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `shipped_at` timestamp NULL DEFAULT NULL,
  `delivered_at` timestamp NULL DEFAULT NULL,
  `refund_requested_at` timestamp NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_order_no` (`order_no`),
  KEY `idx_user` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

DROP TABLE IF EXISTS `tb_order_item`;
CREATE TABLE `tb_order_item` (
  `id` bigint UNSIGNED NOT NULL AUTO_INCREMENT,
  `order_no` varchar(32) NOT NULL,
  `product_id` bigint NOT NULL,
  `name` varchar(128) NOT NULL,
  `sku` varchar(64) DEFAULT NULL,
  `quantity` int NOT NULL DEFAULT 1,
  `price` bigint NOT NULL COMMENT '成交单价(分)',
  PRIMARY KEY (`id`),
  KEY `idx_order_no` (`order_no`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

DROP TABLE IF EXISTS `tb_shipment`;
CREATE TABLE `tb_shipment` (
  `tracking_number` varchar(64) NOT NULL,
  `carrier` varchar(32) DEFAULT NULL,
  `status` varchar(24) DEFAULT NULL COMMENT 'in_transit/delivered',
  PRIMARY KEY (`tracking_number`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

DROP TABLE IF EXISTS `tb_logistics_event`;
CREATE TABLE `tb_logistics_event` (
  `id` bigint UNSIGNED NOT NULL AUTO_INCREMENT,
  `tracking_number` varchar(64) NOT NULL,
  `seq` int NOT NULL,
  `time` varchar(32) DEFAULT NULL,
  `location` varchar(64) DEFAULT NULL,
  `description` varchar(128) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_tn` (`tracking_number`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 种子:商品(price/floor_price 单位=分)
INSERT INTO `tb_product` (title,category,images,price,floor_price,stock,sku,description,specs) VALUES
('Nike Air Max 270 运动鞋','运动鞋','/imgs/products/nike270.jpg',89900,75000,156,'SHOE-270-BK-42','经典气垫缓震,透气网面','{"颜色":"黑色","尺码":"42"}'),
('小米14 Ultra 手机','手机','/imgs/products/mi14u.jpg',599900,560000,42,'PHONE-MI14U-BK','骁龙8Gen3,徕卡四摄','{"颜色":"黑色","存储":"16+512G"}'),
('Apple AirPods Pro 2','耳机','/imgs/products/airpods.jpg',179900,NULL,89,'ELEC-APP-002','主动降噪,USB-C','{"颜色":"白色"}'),
('戴森 V15 吸尘器','家电','/imgs/products/dyson.jpg',429900,400000,23,'HOME-DYSON-V15','激光探测微尘','{"颜色":"金色"}');

-- 种子:一条已发货订单(user_id=1 需存在于 tb_user;下方以你库里真实 userId 替换)
INSERT INTO `tb_order` (order_no,user_id,status,total,shipping_address,tracking_number,carrier,created_at,shipped_at) VALUES
('ORD-20240115-001',1,'shipped',89900,'上海市浦东新区xx路1号','SF1234567890','顺丰速运','2024-01-15 10:30:00','2024-01-16 14:20:00');
INSERT INTO `tb_order_item` (order_no,product_id,name,sku,quantity,price) VALUES
('ORD-20240115-001',1,'Nike Air Max 270 运动鞋','SHOE-270-BK-42',1,89900);
INSERT INTO `tb_shipment` (tracking_number,carrier,status) VALUES ('SF1234567890','顺丰速运','in_transit');
INSERT INTO `tb_logistics_event` (tracking_number,seq,time,location,description) VALUES
('SF1234567890',0,'2024-01-16 14:20','深圳南山区','快件已揽收'),
('SF1234567890',1,'2024-01-17 22:00','上海转运中心','已到达'),
('SF1234567890',2,'2024-01-18 08:30','上海浦东区','正在派送中');
```

- [ ] **Step 2: 导入并验证**

Run(Git Bash):
```bash
MYSQL="D:/BtSoft/mysql/MySQL5.7/bin/mysql.exe"
"$MYSQL" -uroot -pRoot@123456 heimadp < "D:/BaiduNetdiskDownload/02-实战篇/代码/hm-dianping/src/main/resources/db/ecommerce.sql"
"$MYSQL" -uroot -pRoot@123456 heimadp -e "SELECT COUNT(*) products FROM tb_product; SELECT order_no,status FROM tb_order;"
```
Expected: products=4;订单 `ORD-20240115-001 shipped`。
> 注:`tb_order.user_id=1` 需对应 `tb_user` 里真实存在的 userId。先 `SELECT id FROM tb_user LIMIT 3;` 取一个真实 id 替换种子里的 `1`,保证归属校验能命中。

- [ ] **Step 3: Commit**（在 hm-dianping 仓库)
```bash
cd "D:/BaiduNetdiskDownload/02-实战篇/代码/hm-dianping"
git add src/main/resources/db/ecommerce.sql && git commit -m "feat(ecom): 新增电商表与种子数据"
```

## Task 1.2: Product 模块(实体/Mapper/Service/Controller + 查询接口)

**Files:**
- Create: `entity/Product.java` `mapper/ProductMapper.java` `service/IProductService.java` `service/impl/ProductServiceImpl.java` `controller/ProductController.java`

**Interfaces:**
- Produces REST: `GET /product/list?keyword=&current=` → `Result.ok(List<Product>)`;`GET /product/{id}` → `Result.ok(Product)`。返回体沿用 hmdp 的 `Result{success,data,errorMsg,total}`。**floor_price 不在此剔除**(留给 hmdp-mcp 层剔除,后端保持完整,方便议价接口读取)。

- [ ] **Step 1: 写 Entity**（照 `entity/Shop.java` 骨架）

`entity/Product.java`:
```java
package com.hmdp.entity;
import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;
import lombok.EqualsAndHashCode;
import lombok.experimental.Accessors;
import java.io.Serializable;
import java.time.LocalDateTime;

@Data @EqualsAndHashCode(callSuper = false) @Accessors(chain = true)
@TableName("tb_product")
public class Product implements Serializable {
    private static final long serialVersionUID = 1L;
    @TableId(value = "id", type = IdType.AUTO)
    private Long id;
    private String title;
    private String category;
    private String images;
    private Long price;        // 分
    private Long floorPrice;   // 分,议价底价
    private Integer stock;
    private String sku;
    private String description;
    private String specs;      // JSON 字符串
    private Integer status;
    private LocalDateTime createTime;
    private LocalDateTime updateTime;
}
```

- [ ] **Step 2: 写 Mapper / Service / ServiceImpl（空继承壳）**

`mapper/ProductMapper.java`:
```java
package com.hmdp.mapper;
import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.hmdp.entity.Product;
public interface ProductMapper extends BaseMapper<Product> {}
```
`service/IProductService.java`:
```java
package com.hmdp.service;
import com.baomidou.mybatisplus.extension.service.IService;
import com.hmdp.entity.Product;
public interface IProductService extends IService<Product> {}
```
`service/impl/ProductServiceImpl.java`:
```java
package com.hmdp.service.impl;
import com.baomidou.mybatisplus.extension.service.impl.ServiceImpl;
import com.hmdp.entity.Product;
import com.hmdp.mapper.ProductMapper;
import com.hmdp.service.IProductService;
import org.springframework.stereotype.Service;
@Service
public class ProductServiceImpl extends ServiceImpl<ProductMapper, Product> implements IProductService {}
```

- [ ] **Step 3: 写 Controller**（照 `ShopController` 的分页范式)

`controller/ProductController.java`:
```java
package com.hmdp.controller;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.extension.plugins.pagination.Page;
import com.hmdp.dto.Result;
import com.hmdp.entity.Product;
import com.hmdp.service.IProductService;
import org.springframework.web.bind.annotation.*;
import javax.annotation.Resource;

@RestController
@RequestMapping("/product")
public class ProductController {
    @Resource private IProductService productService;

    @GetMapping("/list")
    public Result list(@RequestParam(value = "keyword", required = false) String keyword,
                       @RequestParam(value = "current", defaultValue = "1") Integer current) {
        LambdaQueryWrapper<Product> w = new LambdaQueryWrapper<>();
        w.eq(Product::getStatus, 1);
        if (keyword != null && !keyword.isEmpty()) {
            w.and(q -> q.like(Product::getTitle, keyword)
                        .or().like(Product::getCategory, keyword)
                        .or().like(Product::getDescription, keyword));
        }
        Page<Product> page = productService.page(new Page<>(current, 10), w);
        return Result.ok(page.getRecords(), page.getTotal());
    }

    @GetMapping("/{id}")
    public Result byId(@PathVariable("id") Long id) {
        Product p = productService.getById(id);
        return p == null ? Result.fail("商品不存在") : Result.ok(p);
    }
}
```

- [ ] **Step 4: 启动 hmdp,验证接口**

在 IntelliJ 重启 `HmDianPingApplication`,然后:
```bash
curl -s "http://127.0.0.1:8085/product/list?keyword=手机" | head -c 300
curl -s "http://127.0.0.1:8085/product/1" | head -c 300
```
Expected: `{"success":true,"data":[{"id":2,"title":"小米14 Ultra 手机",...}]}`;详情返回 id=1 的 Nike。

- [ ] **Step 5: Commit** `git commit -m "feat(ecom): Product 模块与查询接口"`

## Task 1.3: Order 模块(下单/我的订单/详情 + 订单号生成)

**Files:**
- Create: `entity/Order.java` `entity/OrderItem.java` `mapper/OrderMapper.java` `mapper/OrderItemMapper.java` `service/IOrderService.java` `service/impl/OrderServiceImpl.java` `controller/OrderController.java` `dto/PlaceOrderDTO.java`

**Interfaces:**
- Consumes: `UserHolder.getUser().getId()`(当前 hmdp userId);现有 `RedisIdWorker`(生成序号)。
- Produces REST(全部受登录保护,带 `authorization` 头):
  - `POST /order` body `{productId, quantity, address}` → `Result.ok(order_no)`;创建订单,status=`pending`,扣库存,写 order_item。
  - `GET /order/of/me` → `Result.ok(List<OrderVO>)`;当前用户全部订单(含 items)。
  - `GET /order/{orderNo}` → `Result.ok(OrderVO)`;归属校验:非本人返回 fail。
- 数据结构 OrderVO 顶层字段:`orderNo,userId,status,total,shippingAddress,trackingNumber,carrier,items[{name,sku,quantity,price}],createdAt,...`(与 agent 契约同名,方便 MCP 直传)。

- [ ] **Step 1: Entity + DTO**

`entity/Order.java`:
```java
package com.hmdp.entity;
import com.baomidou.mybatisplus.annotation.*;
import lombok.Data; import lombok.EqualsAndHashCode; import lombok.experimental.Accessors;
import java.io.Serializable; import java.time.LocalDateTime;
@Data @EqualsAndHashCode(callSuper=false) @Accessors(chain=true)
@TableName("tb_order")
public class Order implements Serializable {
    private static final long serialVersionUID=1L;
    @TableId(value="id",type=IdType.AUTO) private Long id;
    private String orderNo;
    private Long userId;
    private String status;      // pending/shipped/delivered/refund_processing/cancelled
    private Long total;         // 分
    private String shippingAddress;
    private String trackingNumber;
    private String carrier;
    private String refundReason;
    private String refundStatus;
    private LocalDateTime createdAt;
    private LocalDateTime shippedAt;
    private LocalDateTime deliveredAt;
    private LocalDateTime refundRequestedAt;
}
```
`entity/OrderItem.java`:
```java
package com.hmdp.entity;
import com.baomidou.mybatisplus.annotation.*;
import lombok.Data; import lombok.EqualsAndHashCode; import lombok.experimental.Accessors;
import java.io.Serializable;
@Data @EqualsAndHashCode(callSuper=false) @Accessors(chain=true)
@TableName("tb_order_item")
public class OrderItem implements Serializable {
    private static final long serialVersionUID=1L;
    @TableId(value="id",type=IdType.AUTO) private Long id;
    private String orderNo;
    private Long productId;
    private String name;
    private String sku;
    private Integer quantity;
    private Long price;         // 分
}
```
`dto/PlaceOrderDTO.java`:
```java
package com.hmdp.dto;
import lombok.Data;
@Data
public class PlaceOrderDTO { private Long productId; private Integer quantity; private String address; }
```
Mapper 两个空继承壳(照 Task 1.2 Step 2 的写法,泛型换成 Order / OrderItem)。

- [ ] **Step 2: Service 接口 + 实现(含订单号生成、下单事务)**

`service/IOrderService.java`:
```java
package com.hmdp.service;
import com.baomidou.mybatisplus.extension.service.IService;
import com.hmdp.dto.PlaceOrderDTO; import com.hmdp.dto.Result; import com.hmdp.entity.Order;
public interface IOrderService extends IService<Order> {
    Result placeOrder(PlaceOrderDTO dto);
    Result myOrders();
    Result orderDetail(String orderNo);
}
```
`service/impl/OrderServiceImpl.java`(关键逻辑,订单号 = `ORD-` + yyyyMMdd + `-` + RedisIdWorker 序号后3位):
```java
package com.hmdp.service.impl;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.baomidou.mybatisplus.extension.service.impl.ServiceImpl;
import com.hmdp.dto.PlaceOrderDTO; import com.hmdp.dto.Result;
import com.hmdp.entity.*; import com.hmdp.mapper.OrderMapper;
import com.hmdp.service.*; import com.hmdp.utils.RedisIdWorker; import com.hmdp.utils.UserHolder;
import org.springframework.stereotype.Service; import org.springframework.transaction.annotation.Transactional;
import javax.annotation.Resource;
import java.time.LocalDateTime; import java.time.format.DateTimeFormatter;
import java.util.*;

@Service
public class OrderServiceImpl extends ServiceImpl<OrderMapper, Order> implements IOrderService {
    @Resource private IProductService productService;
    @Resource private OrderItemServiceImpl // 若不建 OrderItem 的 Service,可直接注入 OrderItemMapper
        ; // 见说明:简化用 OrderItemMapper
    @Resource private com.hmdp.mapper.OrderItemMapper orderItemMapper;
    @Resource private RedisIdWorker redisIdWorker;

    @Override @Transactional
    public Result placeOrder(PlaceOrderDTO dto) {
        Long userId = UserHolder.getUser().getId();
        Product p = productService.getById(dto.getProductId());
        if (p == null) return Result.fail("商品不存在");
        int qty = dto.getQuantity() == null ? 1 : dto.getQuantity();
        if (p.getStock() < qty) return Result.fail("库存不足");
        // 扣库存(乐观:where stock>=qty)
        boolean ok = productService.lambdaUpdate()
            .setSql("stock = stock - " + qty)
            .eq(Product::getId, p.getId()).ge(Product::getStock, qty).update();
        if (!ok) return Result.fail("库存不足");
        String orderNo = "ORD-" + LocalDateTime.now().format(DateTimeFormatter.ofPattern("yyyyMMdd"))
            + "-" + String.format("%03d", redisIdWorker.nextId("order") % 1000);
        Order o = new Order().setOrderNo(orderNo).setUserId(userId).setStatus("pending")
            .setTotal(p.getPrice() * qty).setShippingAddress(dto.getAddress())
            .setCreatedAt(LocalDateTime.now());
        save(o);
        OrderItem it = new OrderItem().setOrderNo(orderNo).setProductId(p.getId())
            .setName(p.getTitle()).setSku(p.getSku()).setQuantity(qty).setPrice(p.getPrice());
        orderItemMapper.insert(it);
        return Result.ok(orderNo);
    }

    @Override
    public Result myOrders() {
        Long userId = UserHolder.getUser().getId();
        List<Order> orders = lambdaQuery().eq(Order::getUserId, userId)
            .orderByDesc(Order::getCreatedAt).list();
        return Result.ok(orders.stream().map(this::toVO).collect(java.util.stream.Collectors.toList()));
    }

    @Override
    public Result orderDetail(String orderNo) {
        Long userId = UserHolder.getUser().getId();
        Order o = lambdaQuery().eq(Order::getOrderNo, orderNo).one();
        if (o == null || !o.getUserId().equals(userId)) return Result.fail("未找到订单");
        return Result.ok(toVO(o));
    }

    private Map<String,Object> toVO(Order o) {
        Map<String,Object> m = new LinkedHashMap<>();
        m.put("order_no", o.getOrderNo()); m.put("user_id", o.getUserId());
        m.put("status", o.getStatus()); m.put("total", o.getTotal());
        m.put("shipping_address", o.getShippingAddress());
        m.put("tracking_number", o.getTrackingNumber()); m.put("carrier", o.getCarrier());
        m.put("refund_reason", o.getRefundReason()); m.put("refund_status", o.getRefundStatus());
        m.put("created_at", o.getCreatedAt());
        List<OrderItem> items = orderItemMapper.selectList(
            new LambdaQueryWrapper<OrderItem>().eq(OrderItem::getOrderNo, o.getOrderNo()));
        m.put("items", items);
        return m;
    }
}
```
> 说明:上面 `OrderItemServiceImpl` 那行是占位错误示范——实际**只注入 `OrderItemMapper`**(如代码所示),不必给 OrderItem 建 Service。删掉那两行占位。

- [ ] **Step 3: Controller**

`controller/OrderController.java`:
```java
package com.hmdp.controller;
import com.hmdp.dto.PlaceOrderDTO; import com.hmdp.dto.Result;
import com.hmdp.service.IOrderService;
import org.springframework.web.bind.annotation.*;
import javax.annotation.Resource;
@RestController
@RequestMapping("/order")
public class OrderController {
    @Resource private IOrderService orderService;
    @PostMapping public Result place(@RequestBody PlaceOrderDTO dto){ return orderService.placeOrder(dto); }
    @GetMapping("/of/me") public Result mine(){ return orderService.myOrders(); }
    @GetMapping("/{orderNo}") public Result detail(@PathVariable String orderNo){ return orderService.orderDetail(orderNo); }
}
```

- [ ] **Step 4: 验证(需登录 token)**

先拿一个 token(用前端登录,或直接调 hmdp):
```bash
# 发码(手机号任意合法),从 hmdp 控制台看验证码后登录
curl -s "http://127.0.0.1:8085/user/code?phone=13800138000" >/dev/null
# 看 IntelliJ 控制台的 "发送短信验证码:XXXXXX",填入下行 code
TOKEN=$(curl -s -X POST "http://127.0.0.1:8085/user/login" -H "Content-Type: application/json" -d '{"phone":"13800138000","code":"XXXXXX"}' | python -c "import sys,json;print(json.load(sys.stdin)['data'])")
echo "token=$TOKEN"
curl -s -X POST "http://127.0.0.1:8085/order" -H "authorization: $TOKEN" -H "Content-Type: application/json" -d '{"productId":1,"quantity":1,"address":"上海xx路2号"}'
curl -s "http://127.0.0.1:8085/order/of/me" -H "authorization: $TOKEN" | head -c 400
```
Expected: 下单返回 `{"success":true,"data":"ORD-2026...-NNN"}`;`/order/of/me` 返回该用户订单数组含 items。

- [ ] **Step 5: Commit** `git commit -m "feat(ecom): Order 下单/我的订单/详情"`

## Task 1.4: 写接口 — 退款/取消/改地址(状态机 + 幂等)

**Files:**
- Modify: `service/IOrderService.java` `service/impl/OrderServiceImpl.java` `controller/OrderController.java`

**Interfaces:**
- Produces REST(受登录保护):
  - `POST /order/{orderNo}/refund` body `{reason}` → 幂等:仅当 status ∈ {pending,shipped,delivered} 才置 `refund_processing` + 写 refund_reason/refund_requested_at;已 refund_processing/cancelled 返回幂等提示。
  - `POST /order/{orderNo}/cancel` → 仅当 status==pending 才置 `cancelled`;否则拒。
  - `PUT /order/{orderNo}/address` body `{address}` → 仅 pending 可改;空地址拒。
- 幂等由"状态机 + 归属校验"保证(与 agent 的 consent 门互补:agent 侧先确认,hmdp 侧再校验状态)。

- [ ] **Step 1: Service 方法**（追加到 OrderServiceImpl,均先归属校验)
```java
@Override public Result refund(String orderNo, String reason){
    Order o = ownedOrder(orderNo); if(o==null) return Result.fail("未找到订单");
    if("refund_processing".equals(o.getStatus())) return Result.ok("该订单退款处理中,无需重复申请");
    if("cancelled".equals(o.getStatus())) return Result.fail("订单已取消,款项原路退回,无需重复退款");
    lambdaUpdate().eq(Order::getOrderNo,orderNo)
        .set(Order::getStatus,"refund_processing").set(Order::getRefundStatus,"审核中")
        .set(Order::getRefundReason,reason).set(Order::getRefundRequestedAt,java.time.LocalDateTime.now()).update();
    return Result.ok("退款申请已提交,预计1-3个工作日审核");
}
@Override public Result cancel(String orderNo){
    Order o = ownedOrder(orderNo); if(o==null) return Result.fail("未找到订单");
    if("cancelled".equals(o.getStatus())) return Result.fail("订单已取消");
    if(!"pending".equals(o.getStatus())) return Result.fail("已发货/完成,无法取消");
    lambdaUpdate().eq(Order::getOrderNo,orderNo).set(Order::getStatus,"cancelled").update();
    return Result.ok("订单已取消,若已付款款项原路退回");
}
@Override public Result changeAddress(String orderNo, String address){
    if(address==null||address.trim().isEmpty()) return Result.fail("新地址不能为空");
    Order o = ownedOrder(orderNo); if(o==null) return Result.fail("未找到订单");
    if(!"pending".equals(o.getStatus())) return Result.fail("已进入发货流程,无法改地址");
    lambdaUpdate().eq(Order::getOrderNo,orderNo).set(Order::getShippingAddress,address).update();
    return Result.ok("收货地址已更新");
}
private Order ownedOrder(String orderNo){
    Long uid = UserHolder.getUser().getId();
    Order o = lambdaQuery().eq(Order::getOrderNo,orderNo).one();
    return (o!=null && o.getUserId().equals(uid)) ? o : null;
}
```
(在 `IOrderService` 加对应三个方法声明。)

- [ ] **Step 2: Controller 追加**
```java
@PostMapping("/{orderNo}/refund") public Result refund(@PathVariable String orderNo,@RequestBody java.util.Map<String,String> b){ return orderService.refund(orderNo,b.get("reason")); }
@PostMapping("/{orderNo}/cancel") public Result cancel(@PathVariable String orderNo){ return orderService.cancel(orderNo); }
@PutMapping("/{orderNo}/address") public Result addr(@PathVariable String orderNo,@RequestBody java.util.Map<String,String> b){ return orderService.changeAddress(orderNo,b.get("address")); }
```

- [ ] **Step 3: 验证幂等**（用 Task 1.3 的 TOKEN 和已下单 orderNo）
```bash
ON=ORD-xxxx   # 换成真实订单号
curl -s -X POST "http://127.0.0.1:8085/order/$ON/refund" -H "authorization: $TOKEN" -H "Content-Type: application/json" -d '{"reason":"不想要了"}'
curl -s -X POST "http://127.0.0.1:8085/order/$ON/refund" -H "authorization: $TOKEN" -H "Content-Type: application/json" -d '{"reason":"再退一次"}'
```
Expected: 第一次"退款申请已提交";第二次"退款处理中,无需重复申请"(幂等生效)。

- [ ] **Step 4: Commit** `git commit -m "feat(ecom): 退款/取消/改址写接口(状态机+幂等)"`

## Task 1.5: 物流查询接口

**Files:**
- Create: `entity/Shipment.java` `entity/LogisticsEvent.java` `mapper/ShipmentMapper.java` `mapper/LogisticsEventMapper.java` `controller/LogisticsController.java`

**Interfaces:**
- Produces REST: `GET /logistics/{trackingNumber}` → `Result.ok({tracking_number,carrier,status,events:[{time,location,description}]})`。放行(游客可查)。

- [ ] **Step 1: 两个 Entity + 两个 Mapper 壳**（`@TableName("tb_shipment")` 主键 trackingNumber `IdType.INPUT`;LogisticsEvent 自增。Mapper 空继承 BaseMapper。)

- [ ] **Step 2: Controller**
```java
package com.hmdp.controller;
import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.hmdp.dto.Result; import com.hmdp.entity.*; import com.hmdp.mapper.*;
import org.springframework.web.bind.annotation.*;
import javax.annotation.Resource; import java.util.*;
@RestController @RequestMapping("/logistics")
public class LogisticsController {
    @Resource ShipmentMapper shipmentMapper; @Resource LogisticsEventMapper eventMapper;
    @GetMapping("/{tn}")
    public Result get(@PathVariable("tn") String tn){
        Shipment s = shipmentMapper.selectById(tn);
        if(s==null) return Result.fail("无物流信息");
        List<LogisticsEvent> evs = eventMapper.selectList(
            new LambdaQueryWrapper<LogisticsEvent>().eq(LogisticsEvent::getTrackingNumber,tn).orderByAsc(LogisticsEvent::getSeq));
        Map<String,Object> m=new LinkedHashMap<>();
        m.put("tracking_number",s.getTrackingNumber()); m.put("carrier",s.getCarrier()); m.put("status",s.getStatus());
        m.put("events", evs.stream().map(e->{Map<String,Object> x=new LinkedHashMap<>();
            x.put("time",e.getTime());x.put("location",e.getLocation());x.put("description",e.getDescription());return x;}).collect(java.util.stream.Collectors.toList()));
        return Result.ok(m);
    }
}
```

- [ ] **Step 3: 验证** `curl -s http://127.0.0.1:8085/logistics/SF1234567890` → 含 3 条 events。
- [ ] **Step 4: Commit** `git commit -m "feat(ecom): 物流查询接口"`

## Task 1.6: MvcConfig 放行商品/物流查询

**Files:**
- Modify: `config/MvcConfig.java`(在 `excludePathPatterns(...)` 列表追加 `"/product/**"`, `"/logistics/**"`)

- [ ] **Step 1: 追加放行路径**（下单/我的订单/退款仍受保护,只放行只读浏览)
```java
.excludePathPatterns(
    "/shop/**","/voucher/**","/shop-type/**","/upload/**","/blog/hot",
    "/user/code","/user/login",
    "/product/**","/logistics/**"          // 新增:游客可浏览商品与物流
);
```
- [ ] **Step 2: 验证** 不带 token `curl -s "http://127.0.0.1:8085/product/list"` 返回商品(不再 401);`curl -s "http://127.0.0.1:8085/order/of/me"` 仍 401。
- [ ] **Step 3: Commit** `git commit -m "feat(ecom): 放行商品/物流查询给游客"`

---

# Phase 2 — hmdp-mcp 适配层(Python FastMCP)

在 agent 仓库 `ecom-service-agent` 下开发,用其 `.venv`。

## Task 2.1: hmdp HTTP 客户端 + 字段映射纯函数(先测)

**Files:**
- Create: `mcp_server/hmdp_client.py`
- Create: `mcp_server/hmdp_mapping.py`
- Test: `tests/test_hmdp_mapping.py`

**Interfaces:**
- Produces: `hmdp_client.HmdpClient(base_url).get_json(path, params, token) -> dict`;`post_json(path, body, token) -> dict`(自动带 `authorization` 头)。
- Produces 映射纯函数:`map_product(hmdp_product: dict, public: bool) -> dict`、`map_order(hmdp_order: dict) -> dict`、`map_logistics(hmdp_lg: dict) -> dict`(分→元、字段改名、public=True 时剔除 floor_price)。

- [ ] **Step 1: 写映射纯函数的失败测试**

`tests/test_hmdp_mapping.py`:
```python
from mcp_server.hmdp_mapping import map_product, map_order

def test_map_product_fen_to_yuan_and_hide_floor():
    hp = {"id": 2, "title": "小米14 Ultra 手机", "category": "手机",
          "price": 599900, "floorPrice": 560000, "stock": 42,
          "sku": "PHONE-MI14U-BK", "description": "骁龙8", "specs": '{"颜色":"黑色"}'}
    pub = map_product(hp, public=True)
    assert pub["product_id"] == "2"
    assert pub["name"] == "小米14 Ultra 手机"
    assert pub["price"] == 5999.0            # 分→元
    assert pub["specs"] == {"颜色": "黑色"}    # JSON 串→dict
    assert "floor_price" not in pub          # 公开视图剔除底价
    internal = map_product(hp, public=False)
    assert internal["floor_price"] == 5600.0 # 内部保留(议价用)

def test_map_order_fields_and_items():
    ho = {"order_no": "ORD-20240115-001", "user_id": 1, "status": "shipped",
          "total": 89900, "shipping_address": "上海", "tracking_number": "SF1",
          "carrier": "顺丰", "items": [{"name": "鞋", "sku": "S1", "quantity": 1, "price": 89900}]}
    o = map_order(ho)
    assert o["order_id"] == "ORD-20240115-001"
    assert o["user"] == "1"                   # 归属字段=字符串 userId
    assert o["status"] == "shipped"
    assert o["total"] == 899.0
    assert o["items"][0]["price"] == 899.0    # item 价也分→元
```

- [ ] **Step 2: 运行,确认失败** `./.venv/Scripts/python.exe -m pytest tests/test_hmdp_mapping.py -q` → ImportError/FAIL。

- [ ] **Step 3: 实现映射 + 客户端**

`mcp_server/hmdp_mapping.py`:
```python
import json
def _yuan(fen):
    return round((fen or 0) / 100, 2)
def map_product(hp: dict, public: bool = True) -> dict:
    specs = hp.get("specs")
    try: specs = json.loads(specs) if isinstance(specs, str) else (specs or {})
    except (ValueError, TypeError): specs = {}
    out = {"product_id": str(hp.get("id")), "name": hp.get("title"),
           "category": hp.get("category"), "price": _yuan(hp.get("price")),
           "stock": hp.get("stock"), "description": hp.get("description") or "", "specs": specs}
    if not public:
        out["floor_price"] = _yuan(hp.get("floorPrice"))
    return out
def map_order(ho: dict) -> dict:
    items = [{"name": it.get("name"), "sku": it.get("sku"),
              "quantity": it.get("quantity"), "price": _yuan(it.get("price"))}
             for it in (ho.get("items") or [])]
    return {"order_id": ho.get("order_no"), "user": str(ho.get("user_id")),
            "status": ho.get("status"), "total": _yuan(ho.get("total")),
            "shipping_address": ho.get("shipping_address"),
            "tracking_number": ho.get("tracking_number"), "carrier": ho.get("carrier"),
            "refund_reason": ho.get("refund_reason"), "refund_status": ho.get("refund_status"),
            "items": items, "created_at": ho.get("created_at")}
def map_logistics(hl: dict) -> dict:
    return {"tracking_number": hl.get("tracking_number"), "carrier": hl.get("carrier"),
            "status": hl.get("status"), "events": hl.get("events") or []}
```
`mcp_server/hmdp_client.py`:
```python
import httpx
class HmdpClient:
    def __init__(self, base_url="http://127.0.0.1:8085", timeout=8.0):
        self.base_url = base_url.rstrip("/"); self.timeout = timeout
    def _headers(self, token): return {"authorization": token} if token else {}
    def get_json(self, path, params=None, token=None):
        r = httpx.get(self.base_url + path, params=params, headers=self._headers(token), timeout=self.timeout)
        return r.json()
    def post_json(self, path, body=None, token=None):
        r = httpx.post(self.base_url + path, json=body or {}, headers=self._headers(token), timeout=self.timeout)
        return r.json()
    def put_json(self, path, body=None, token=None):
        r = httpx.put(self.base_url + path, json=body or {}, headers=self._headers(token), timeout=self.timeout)
        return r.json()
```

- [ ] **Step 4: 运行,确认通过** `pytest tests/test_hmdp_mapping.py -q` → PASS。
- [ ] **Step 5: Commit**（agent 仓库,新分支)`git commit -m "feat(hmdp-mcp): hmdp 客户端 + 字段映射纯函数"`

## Task 2.2: MCP server 骨架 + 只读工具(商品/订单/物流/我的订单)

**Files:**
- Create: `mcp_server/hmdp_server.py`
- Test: `tests/test_hmdp_server_tools.py`

**Interfaces:**
- Consumes: `HmdpClient`, `map_*`;`app.agent.runtime_context.set_current_user`。
- Produces MCP 工具(FastMCP,:9123,返回 json 字符串,带 `ctx_user_id`):`query_product`、`query_order`、`list_user_orders`、`query_logistics`。
- **身份**:`ctx_user_id` 即 hmdp userId;工具用它做归属过滤,并作为调用 hmdp 的凭据来源(见 Task 3.2:userId→token 反查,或直接用 userId 过滤只读)。此阶段先按"MCP server 直连 hmdp 只读接口 + 用 ctx_user_id 过滤订单归属"实现。

- [ ] **Step 1: 写工具的集成测试(mock HmdpClient)**

`tests/test_hmdp_server_tools.py`:
```python
import json
from unittest.mock import patch
import mcp_server.hmdp_server as srv

def test_query_product_hides_floor():
    fake = {"success": True, "data": [{"id": 2, "title": "小米14 Ultra 手机",
            "category": "手机", "price": 599900, "floorPrice": 560000, "stock": 42,
            "sku": "X", "description": "d", "specs": "{}"}]}
    with patch.object(srv._client, "get_json", return_value=fake):
        out = json.loads(srv._query_product_impl("手机", ctx_user_id="1"))
    assert out["success"] and out["products"][0]["price"] == 5999.0
    assert "floor_price" not in out["products"][0]

def test_query_order_ownership():
    order = {"success": True, "data": {"order_no": "ORD-1", "user_id": 1, "status": "shipped",
             "total": 89900, "items": []}}
    with patch.object(srv._client, "get_json", return_value=order):
        ok = json.loads(srv._query_order_impl("ORD-1", ctx_user_id="1"))
        bad = json.loads(srv._query_order_impl("ORD-1", ctx_user_id="999"))
    assert ok["success"] and ok["order"]["status"] == "shipped"
    assert bad["success"] is False        # 非本人 → 查无此单
```

- [ ] **Step 2: 运行,确认失败。**

- [ ] **Step 3: 实现 server(impl 函数与 @mcp.tool 分离,便于单测)**

`mcp_server/hmdp_server.py`:
```python
import json
from mcp.server.fastmcp import FastMCP
from mcp_server.hmdp_client import HmdpClient
from mcp_server.hmdp_mapping import map_product, map_order, map_logistics
from app.agent.runtime_context import set_current_user

_client = HmdpClient()
mcp = FastMCP("hmdp-ecom", host="127.0.0.1", port=9123)

def _dump(d): return json.dumps(d, ensure_ascii=False)

def _query_product_impl(keyword: str, ctx_user_id: str = "") -> str:
    set_current_user(ctx_user_id or None)
    res = _client.get_json("/product/list", params={"keyword": keyword})
    if not res.get("success"): return _dump({"success": False, "error": res.get("errorMsg") or "查询失败"})
    prods = [map_product(p, public=True) for p in (res.get("data") or [])]
    return _dump({"success": True, "products": prods})

def _query_order_impl(order_id: str, ctx_user_id: str = "") -> str:
    set_current_user(ctx_user_id or None)
    res = _client.get_json(f"/order/{order_id}")
    if not res.get("success") or not res.get("data"):
        return _dump({"success": False, "error": f"未找到订单 {order_id}"})
    o = map_order(res["data"])
    if ctx_user_id and o.get("user") != str(ctx_user_id):
        return _dump({"success": False, "error": f"未找到订单 {order_id}"})   # 归属:非本人=查无
    return _dump({"success": True, "order": o})

def _list_user_orders_impl(ctx_user_id: str = "") -> str:
    set_current_user(ctx_user_id or None)
    if not ctx_user_id: return _dump({"success": False, "error": "未识别用户"})
    res = _client.get_json("/order/of/me", token=_token_for(ctx_user_id))
    orders = [map_order(o) for o in (res.get("data") or [])] if res.get("success") else []
    brief = [{"order_id": o["order_id"], "status": o["status"], "total": o["total"]} for o in orders]
    return _dump({"success": True, "count": len(brief), "orders": brief})

def _query_logistics_impl(order_id: str, ctx_user_id: str = "") -> str:
    set_current_user(ctx_user_id or None)
    od = _client.get_json(f"/order/{order_id}")
    if not od.get("success") or not od.get("data"): return _dump({"success": False, "error": "未找到订单"})
    o = map_order(od["data"])
    if ctx_user_id and o.get("user") != str(ctx_user_id): return _dump({"success": False, "error": "未找到订单"})
    tn = o.get("tracking_number")
    if not tn: return _dump({"success": False, "error": "订单尚未发货,暂无物流"})
    lg = _client.get_json(f"/logistics/{tn}")
    if not lg.get("success"): return _dump({"success": False, "error": "暂无物流信息"})
    return _dump({"success": True, "logistics": map_logistics(lg["data"])})

# token 解析见 Task 3.2;此处先桩实现(阶段3替换为共享 Redis 反查)
def _token_for(user_id: str) -> str:
    return ""   # TODO Phase3: userId→token(共享 Redis 反查),/order/of/me 需登录态

@mcp.tool()
def query_product(keyword: str, ctx_user_id: str = "") -> str:
    """根据关键词或商品ID查询商品信息(价格/库存/规格)。"""
    return _query_product_impl(keyword, ctx_user_id)

@mcp.tool()
def query_order(order_id: str, ctx_user_id: str = "") -> str:
    """查询指定订单的状态与明细。"""
    return _query_order_impl(order_id, ctx_user_id)

@mcp.tool()
def list_user_orders(ctx_user_id: str = "") -> str:
    """列出当前用户的全部订单概要。"""
    return _list_user_orders_impl(ctx_user_id)

@mcp.tool()
def query_logistics(order_id: str, ctx_user_id: str = "") -> str:
    """查询指定订单的物流轨迹。"""
    return _query_logistics_impl(order_id, ctx_user_id)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
```

- [ ] **Step 4: 运行测试通过。** `pytest tests/test_hmdp_server_tools.py -q` → PASS。
- [ ] **Step 5: Commit** `git commit -m "feat(hmdp-mcp): 只读工具 query_product/order/logistics/list_user_orders"`

## Task 2.3: 写工具 + 议价(apply_refund/cancel_order/change_address/negotiate_price)

**Files:**
- Modify: `mcp_server/hmdp_server.py`
- Modify: `tests/test_hmdp_server_tools.py`

**Interfaces:**
- Produces MCP 工具:`apply_refund(order_id,reason)`、`cancel_order(order_id)`、`change_address(order_id,new_address)`、`query_coupons()`、`negotiate_price(product_id,buyer_offer)`。写操作调 hmdp 的 `POST /order/{no}/refund` 等(需登录 token,见 Task 3.2)。`negotiate_price` 用 `map_product(public=False)` 拿 floor_price 本地算价(复用 agent `app.agent.tools.bargain.compute_offer`),**返回体不含 floor_price**。

- [ ] **Step 1: 追加议价映射测试**（用 agent 现成 `compute_offer` 保证不破底、逐轮让价;断言返回无 floor_price)。
- [ ] **Step 2: 实现**(写操作 impl:`_client.post_json(f"/order/{order_id}/refund", {"reason":reason}, token=_token_for(ctx_user_id))`,把 hmdp 返回的 `errorMsg/data` 转成 agent 的 `{success,message}`;`negotiate_price` 调 `/product/{id}` 取 hmdp 商品→`map_product(public=False)`→`compute_offer(list_price, floor_price, buyer_offer, rounds)`,rounds 先固定 0 或走 hmdp 侧记录)。
- [ ] **Step 3: 测试通过。**
- [ ] **Step 4: Commit** `git commit -m "feat(hmdp-mcp): 退款/取消/改址/议价工具"`

---

# Phase 3 — 打通验证(agent 接 hmdp-mcp)

## Task 3.1: 放宽订单号正则 + 开启 MCP

**Files:**
- Modify: `app/agent/tools/validation.py`
- Modify: `.env`

**Interfaces:**
- Consumes: 无。Produces: 本地工具路径不再因 hmdp 订单号格式被拒;agent 走 MCP 到 hmdp-mcp。

- [ ] **Step 1: 写正则放宽的失败测试**

在 `tests/test_tool_validation.py` 追加:
```python
def test_ord_format_accepts_hmdp_style():
    from app.agent.tools.validation import validate_tool_args
    # hmdp 订单号仍是 ORD-yyyyMMdd-NNN,应通过
    assert validate_tool_args("query_order", {"order_id": "ORD-20260728-007"}) is None
```
（若 hmdp 采用了 `ORD-\d{8}-\d{3}` 格式,现有正则已兼容,此测试应直接通过;若你改用纯数字/雪花 id,则把 `validation._ORDER_ID_RE` 放宽为 `^(ORD-\d{8}-\d{3}|\d+)$` 并加对应断言。）

- [ ] **Step 2: 按需放宽 `_ORDER_ID_RE`**（仅当 hmdp 不用 ORD 格式时才改;本方案 Task 1.3 生成的是 ORD 格式,通常无需改)。
- [ ] **Step 3: 改 `.env`** 设 `MCP_ENABLED=true`、`MCP_SERVER_URL=http://127.0.0.1:9123/mcp`;确认 `AUTH_ENABLED=true`。
- [ ] **Step 4: 运行相关测试通过。**
- [ ] **Step 5: Commit** `git commit -m "chore(agent): 开启 MCP 接 hmdp-mcp,兼容订单号格式"`

## Task 3.2: 身份打通(会话用户 ↔ hmdp userId / token)

**Files:**
- Create: `app/api/hmdp_identity.py`
- Modify: `mcp_server/hmdp_server.py`(`_token_for` 实到)
- Test: `tests/test_hmdp_identity.py`

**Interfaces:**
- Produces: `resolve_hmdp_user(token) -> str | None`(读共享 Redis Hash `login:token:{token}` 的 `id` 字段);`token_for_user(user_id) -> str | None`(反向:遍历/维护映射;或在会话建立时落一张 `userId→token` 映射表,推荐)。
- 决策:**写操作需要 hmdp 登录态**。最简做法——agent 的 API 层在 `/api/chat` 收到前端传来的 hmdp token 时,一并把 token 存进当轮上下文,MCP 透传 `ctx_token`(除 `ctx_user_id` 外再加一个保留参数),`hmdp_server` 用它调写接口。**若不想扩透传参数**,则 MCP server 用 `ctx_user_id` 去共享 Redis 反查该用户当前有效 token。

- [ ] **Step 1: 写 resolve_hmdp_user 的测试**(用 fakeredis 塞一个 `login:token:tk1` Hash `{id:'5',nickName:'x'}`,断言 `resolve_hmdp_user('tk1')=='5'`)。
- [ ] **Step 2: 实现**(用 `redis` 连 `127.0.0.1:6379`,`hget('login:token:'+token,'id')`)。
- [ ] **Step 3: 打通 `_token_for`**:在 `hmdp_server` 维护/查询 `userId→token`(简化:MCP server 也接受 `ctx_token` 保留参数,由 agent manager 透传;需在 `app/agent/tools/manager.py` 的 MCP 分支补 `ctx_token`——这是 agent 端唯一的小改动)。
- [ ] **Step 4: 测试通过。**
- [ ] **Step 5: Commit** `git commit -m "feat: 会话用户↔hmdp userId/token 打通"`

## Task 3.3: 端到端验证(登录→问客服→查单/物流/退款)

**Files:** 无新增(集成验证)。

- [ ] **Step 1: 起全套**：hmdp 后端(8085,IntelliJ)、hmdp-nginx(8080)、hmdp-mcp(`python mcp_server/hmdp_server.py`,9123)、agent(`python run_api.py`,8010)。
- [ ] **Step 2: 造数据**:前端 8080 登录一个手机号→拿 userId;给该 userId 在 `tb_order` 造 1-2 条不同状态订单(或前端下单)。
- [ ] **Step 3: 驱动 agent**(带该用户身份):对 agent `/api/chat` 依次问:"我的订单"、"ORD-xxx 到哪了"(物流)、"我要退 ORD-xxx"(确认流→重放→hmdp 退款)。断言:agent 事件流里 `tool_call` 命中 MCP 工具、`tool_result` 是 hmdp 真实数据、退款后 hmdp `tb_order.status` 变 `refund_processing`。
- [ ] **Step 4: 验证归属**:用 A 用户身份问 B 用户的订单号 → agent 答"未找到订单"(fail-closed 生效)。
- [ ] **Step 5: 记录结果**（截图/日志)写入 `docs/hmdp-integration-verify.md`。

---

# Phase 4 — 前端商城页

目录:`C:\Users\pb166\hmdp-nginx\nginx-1.18.0\html\hmdp`。复用 `common.js`(axios base `/api` + 自动带 `authorization` token)。改完 nginx 无需重启(静态文件,刷新即生效);仅改端口/conf 才 reload。

## Task 4.1: 商品列表页

**Files:** Create `product-list.html`（复制 `shop-list.html` 改造)

- [ ] **Step 1:** 复制 `shop-list.html` → `product-list.html`;引入 5 个 script(`vue.js→axios.min.js→element.js→common.js→footer.js`)。
- [ ] **Step 2:** `data` 放 `products:[], params:{keyword:'', current:1}`;`created()` 调 `queryProducts()`;`queryProducts(){ axios.get("/product/list",{params:this.params}).then(({data})=>{ this.products=this.products.concat(data) }) }`。
- [ ] **Step 3:** 模板 `v-for="p in products"` 渲染 `p.title / (p.price) / p.images`,`@click="toDetail(p.product_id||p.id)"` → `location.href="/product-detail.html?id="+id`。触底分页照 shop-list 的 onScroll。
- [ ] **Step 4:** 验证:浏览器开 `http://localhost:8080/product-list.html`,看到 4 个商品;点进详情跳转正确。
- [ ] **Step 5:** Commit（若前端在 git 下)。

## Task 4.2: 商品详情 + 下单

**Files:** Create `product-detail.html`（复制 `shop-detail.html`）

- [ ] **Step 1:** `created()`:`let id=util.getUrlParam("id"); axios.get("/product/"+id).then(({data})=>this.product=data)`。
- [ ] **Step 2:** 下单按钮参考 `shop-detail.html` 的 `seckill()`:检查 `sessionStorage.token`(无则提示登录跳 `/login.html`),`axios.post("/order",{productId:id,quantity:1,address:this.address}).then(({data})=>this.$message.success("下单成功,订单号:"+data))`。
- [ ] **Step 3:** 验证:登录后在详情页下单,提示订单号;`tb_order` 出现新记录。

## Task 4.3: 我的订单页

**Files:** Create `my-order.html`（复制带 footBar 的 `info.html` 结构)

- [ ] **Step 1:** `created()`:`axios.get("/order/of/me").then(({data})=>this.orders=data)`(未登录时 401 拦截器自动跳登录)。
- [ ] **Step 2:** 模板渲染订单号/状态/金额/下单时间列表。
- [ ] **Step 3:** 验证:登录后开 `my-order.html` 看到自己的订单。

## Task 4.4: 底部导航加"商城"入口

**Files:** Modify `js/footer.js`

- [ ] **Step 1:** 在 `footBar` 组件 `toPage(i)` 里把某个占位格(如"地图"或"消息")接上 `location.href="/product-list.html"`,或新增一个"商城"格并加分支。
- [ ] **Step 2:** 验证:首页底部点"商城"进入商品列表;所有引 footer.js 的页面同步生效。
- [ ] **Step 3:** Commit。

---

## Self-Review(方案自查)

- **Spec 覆盖**:建表(1.1)、商品(1.2)、订单+状态机(1.3/1.4)、物流(1.5)、放行(1.6)、hmdp-mcp 映射与工具(2.x)、订单号兼容与 MCP 开关(3.1)、身份打通(3.2)、端到端(3.3)、前端三页+导航(4.x)——四阶段全覆盖。
- **契约一致性**:hmdp `tb_order.status` 直接用 agent 英文枚举,`OrderVO`/`map_order` 输出字段名与 agent 契约(order_id/user/status/items[{name,sku,quantity,price}]/tracking_number...)逐一对齐;金额统一分→元于 MCP 层;floor_price 仅在 `map_product(public=False)` 出现且不进任何面向模型的返回。
- **身份**:agent user_id = hmdp userId(字符串)= tb_order.user_id = ctx_user_id,全链路同一命名空间;归属校验 hmdp 侧(ownedOrder)+ MCP 侧(user 比对)双保险。
- **已知取舍/待定**:写操作需 hmdp 登录 token,Task 3.2 给了"扩 ctx_token 透传"或"共享 Redis 反查"两条路,执行时二选一;`negotiate_price` 的 rounds 若要逐轮递减,需 hmdp 侧存议价轮次(可后续增强,先固定 0 也能演示)。

## 执行方式

方案已存于 `docs/superpowers/plans/2026-07-28-hmdp-ecommerce-integration.md`。建议按阶段用 subagent-driven-development 逐任务推进,每个 Task 结束即验证(Java 用 curl、Python 用 pytest、前端用浏览器)。
