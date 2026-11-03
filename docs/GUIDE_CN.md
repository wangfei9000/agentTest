# KsanaTalk 组件书写规范

> **文档版本**: 2.3（独立版）  
> **最后更新**: 2026-06-25  
> **适用版本**: KsanaTalk Client v9.0.0  
> **目标读者**: 对项目零了解的组件开发者  
> **说明**: 本文档自包含全部必要信息，可脱离代码仓库单独使用。

---

## 目录

1. [什么是组件](#1-什么是组件)
2. [执行模型](#2-执行模型必须先理解)
3. [组件脚本基本格式](#3-组件脚本的基本格式)
4. [组件元数据与组织](#4-组件元数据与组织)
5. [返回值约定](#5-返回值约定)
6. [安全编码规范](#6-安全编码规范)
7. [动态实体 DSL 参考](#7-动态实体-dsl-参考)（含 [7.1 引导宏详解](#71-引导宏详解)、[7.7 实体生命周期](#77-api-方法使用注意事项实体生命周期)）
8. [GUI 生成方法详解](#8-gui-生成方法详解)
9. [Team 团队协作模式](#9-team-团队协作模式)
10. [沙箱内置 API 速查](#10-沙箱内置-api-速查)
11. [系统配置与执行限额](#11-系统配置与执行限额)
12. [故障排查](#12-故障排查)
13. [功能示例（t1–t16）](#13-功能示例t1t16)
14. [面板与组件对应关系](#14-面板与组件的对应关系)
15. [按钮条件脚本](#15-按钮条件脚本)
16. [工作流组件](#16-工作流组件)
17. [快速对照](#17-快速对照我该写哪种组件)
18. [检查清单](#18-检查清单提交前自检)
19. [最小可运行示例](#19-最小可运行示例)

---

## 1. 什么是组件

在 KsanaTalk 中，**组件（Component）** 是一段存储在数据库中的 **Groovy 闭包脚本**。系统通过 `ComponentService` 在沙箱（Sandbox）环境中编译并执行它，用于驱动：

- 页面面板：表单渲染、表格数据、搜索条件、创建/更新
- 按钮：可见性条件、点击后跳转逻辑
- 工作流：阶段事件（同步/异步）
- 通用业务：任意需要后端脚本扩展的场景

组件不是独立的 `.groovy` 文件，而是 `Component` 域对象上的 `script` 字段。开发时可以把脚本写在本地文件里调试，最终粘贴或导入到系统中。

### 1.1 核心概念

| 概念 | 说明 |
|------|------|
| **FlexObject** | 动态实体元数据，定义表结构、字段、页面行为 |
| **Territory** | 租户/组织，组件按 Territory 隔离，支持向上继承查找 |
| **Panel** | 页面面板，通过 5 类组件字段绑定不同脚本 |
| **Sandbox** | 受限 Groovy 执行环境，提供 `_` 前缀安全 API |
| **EntityContext** | `_with_entity*` 闭包内的 DSL 运行时，提供 CRUD 与 GUI 方法 |

---

## 2. 执行模型（必须先理解）

### 2.1 调用链

```
HTTP 请求 / 工作流触发
    → ComponentService.evaluate(component, object)
        → 解析引用组件（Library 依赖）
        → 拼接完整脚本：${依赖脚本}(${组件脚本})(object, user)
        → ScriptCompilationService 编译（带 SecureASTCustomizer）
        → GroovyShellBindings 注入沙箱 API
        → ScriptExecutionService 在线程池中执行（超时 + 资源配额）
        → 返回结果给调用方（JSON / 跳转 URL 等）
```

### 2.2 三个公开执行入口

| 方法 | 用途 | 超时默认 |
|------|------|----------|
| `evaluate(component, object)` | 同步执行面板/工作流/通用组件 | 60 秒 |
| `evaluateAsync(component, object)` | 异步火忘执行（工作流异步事件） | 60 秒 |
| `evaluateButtonScript(condition, id, user)` | 按钮可见性条件 | 3 秒 |

### 2.3 两个入参：`object` 与 `user`

| 入参 | 类型 | 说明 |
|------|------|------|
| `object` | `Map` 或 `WorkflowInstance` 等 | 调用方传入的上下文，**面板脚本中通常叫 `parameters`** |
| `user` | `User` | 当前登录用户 |

面板控制器传入的 `object` 典型结构：

```groovy
// loadField
[panel: panel, objectId: objectId, object: object]

// loadData
[layoutPanel: layoutPanel, panel: panel, page: page, objectId: objectId, object: object]

// createData
[panel: panel, object: object]   // object 是前端提交的 JSON 字符串

// updateData
[panel: panel, objectId: objectId, object: object]
```

工作流事件传入的 `object` 是 `WorkflowInstance` 实例。按钮条件脚本签名为 `{ id, user -> ... }`，其中 `id` 是当前页面的 objectId。

### 2.4 沙箱运行时架构

沙箱采用 **Context-Holder 模式**：通过 `ThreadLocal` 在脚本全生命周期传递资源计数器，确保单个脚本无法耗尽服务器资源。

| 组件 | 职责 |
|------|------|
| `SandboxRuntimeContext` | 维护 DB/HTTP/File/Email/SMS/AI 使用计数 |
| `SandboxRuntimeContextHolder` | ThreadLocal 存取当前线程 Context |
| `SandboxLimitExceededException` | 配额超限熔断异常 |
| `ScriptCompilationService` | 脚本编译与 LRU 缓存，ClassLoader 自动回收防 Metaspace 泄漏 |
| `ScriptExecutionService` | 托管线程池，超时、内存检查、并发熔断 |
| `SandboxSecurityService` | 文件 IO 与 URL 读取的统一校验入口 |
| `SecureASTCustomizer` | 编译期禁止危险类导入与使用 |

每次执行前 `ComponentService` 初始化 Context，执行后 `try-finally` 清理。

### 2.5 资源配额（单次脚本）

| 资源 | 默认上限 | 超出异常 |
|------|----------|----------|
| 数据库连接 | 5 | `SandboxLimitExceededException` |
| HTTP 出站 | 5 | 同上 |
| 文件操作 | 20 | 同上 |
| 邮件 / 短信 / AI | 各 5 | 同上 |

编译期由 `SecureASTCustomizer` 禁止 `java.io.File`、`java.net.*`、`java.sql.*` 等——**必须使用沙箱内置函数**。

---

## 3. 组件脚本的基本格式

### 3.1 标准闭包签名

**面板 / 工作流 / 通用组件：**

```groovy
{ parameters, user ->
    // 业务逻辑
    return result
}
```

规则：

1. **第一行必须是闭包头** `{ parameters, user ->`
2. **用 `return` 返回结果**（Map、List、Boolean 等）
3. **不要写 `package`**（编译器禁止）
4. **不要 `import java.io.*` 等危险类**，改用沙箱 API

### 3.2 进入实体上下文

```groovy
_with_entity(294874L) { ... }           // 按 FlexObject 主键 ID
_with_entity('Order') { ... }           // 按 FlexObject code
_with_entity_by_id(294874L) { ... }     // 同上（显式命名）
_with_entity_by_name('Order') { ... }   // 按名称

_with_entity_readonly(294874L) { ... }  // 只读，性能提升约 40%
_with_entity_by_id_readonly(294874L) { ... }
_with_entity_by_name_readonly('Order') { ... }

_with_gorm_transaction { ... }          // 系统域 GORM 事务（非动态实体数据）
```

> **模板占位符**：`${thisFlexObject?.id}` 在代码生成时替换为具体 ID；手写时直接写 ID 或 code。  
> **引导宏选型、嵌套限制、与 `_with_gorm_transaction` 区别** → 见 [§7.1](#71-引导宏详解)。

### 3.3 闭包内可用变量

| 变量 | 说明 |
|------|------|
| `clazz` | 当前 FlexObject 对应的动态实体 Class |
| `flexObject` | FlexObject 元数据（`parent`、`team`、`canDelete` 等） |
| `fields` | 字段定义列表 |
| `indexLayout` / `showLayout` / `layout` | 布局引用（代码生成时注入） |

闭包外可直接使用 `user` 及所有 `_` 前缀沙箱函数。

---

## 4. 组件元数据与组织

### 4.1 Component 关键字段

| 字段 | 说明 |
|------|------|
| `name` | 组件名，**同一 Territory 下唯一** |
| `code` | UUID，系统自动生成 |
| `script` | Groovy 脚本正文 |
| `type` | 组件类型 |
| `territory` | 所属租户，支持向上继承查找 |
| `active` | 是否启用 |
| `message` | 执行成功时的默认提示 |

### 4.2 组件类型

| 类型名 | 用途 |
|--------|------|
| **Territory Condition** | 面板/页面业务脚本（最常见） |
| **Library** / **System Library** / **Territory Library** | 公共库，被其他组件引用，不单独执行 |
| 其他 | 工作流条件、定时任务等 |

### 4.3 引用组件（Library 依赖）

通过 `ReferenceComponent` 关联。编译前将引用脚本**拼接在主体之前**（单层，不递归）：

```
[Library A 脚本]

[Library B 脚本]

{ parameters, user -> ... 主体 ... }
```

脚本 + 依赖总量不得超过 `scriptMaxSize`（默认 100000 字符）。

### 4.4 Territory 查找

从当前 Territory 沿 `parent` 向上查找同名组件，最大深度 `territoryMaxDepth`（默认 20）。

---

## 5. 返回值约定

| 场景 | 返回格式 |
|------|----------|
| 面板 JSON | 任意 Map / List，直接渲染 |
| 成功 + 跳转 | `[status: true, url: "..."]` 或 `targetUrl` |
| 失败 | `[status: false, message: "..."]` |
| 表单 Schema | `form()` / `gridColumns()` / `gridSearchSchema()` 返回的 Map |
| 表格数据 | `gridData()` 返回含 `source`、`paging`、`count` 的 Map |

成功跳转时，系统依次读取 `targetUrl`、`url` 字段决定重定向地址。

---

## 6. 安全编码规范

### 6.1 必须做

**1. 清理前端保留字段（防 Mass Assignment）：**

```groovy
params.remove("id")
params.remove("userId")
params.remove("parentId")
```

**2. 强制写入系统字段：**

```groovy
def baseFields = [userId: user.id as Long]
```

**3. 查询加 `fixedQuery` 围栏：**

```groovy
return gridData(parameters, [fixedQuery: [userId: user.id as Long]])
```

**4. Team 模式使用 `withUserFilter()`**（详见第 9 节）。

**5. 使用沙箱函数**访问网络、文件、数据库、附件。

**6. 只读阶段先 `toMap` 再校验/组装；写入阶段在当次 `_with_entity` 内重新 `get`：**

```groovy
// 只读：查询后立刻脱管，再过滤、校验、返回
def rows = toMapList(find([status: 'A']))
def safe = rows.findAll { it.amount > 0 }

// 写入：禁止沿用只读阶段或闭包外的实体，必须重新加载
def entity = get(clazz, objectId)
entity = bind(entity, params)
update(entity)
```

详见 [§7.7 API 方法使用注意事项](#77-api-方法使用注意事项实体生命周期)。

### 6.2 禁止做

| 禁止 | 原因 |
|------|------|
| `new File(...)`、`Runtime.getRuntime()` | 编译期 AST 拦截 |
| `import java.sql.*` | 编译期拦截 |
| 无限循环 / 大量递归 | 超时（默认 60s） |
| 异步路径依赖懒加载 GORM 关联 | 异步线程无 HTTP Session |
| 返回 Hibernate 托管实体 | LazyInitializationException |
| 只读闭包查出的实体直接 `bind` / `update` | Session 已关闭或实体已脱管，写入失败 |
| 跨 `_with_entity*` 块传递实体引用 | 新块内 Session 不同，实体处于 detached 状态 |
| 在 `toMap` 之前访问懒加载关联字段 | `LazyInitializationException` |

### 6.3 编译期禁止的类（摘要）

以下类别在 `ksanatalk.scriptSecurity.disallowedClasses` 中配置，**编译即失败**：

| 类别 | 示例 |
|------|------|
| 网络 | `java.net.*`、`okhttp3.*`、`org.apache.http.*` |
| 数据库直连 | `java.sql.*`、`groovy.sql.*`、`org.hibernate.*` |
| 文件系统 | `java.io.File`、`java.nio.file.*` |
| 进程/反射 | `java.lang.Runtime`、`java.lang.ProcessBuilder`、`java.lang.reflect.*` |
| 云 SDK | `com.aliyun.*`、`software.amazon.*` |
| Spring 内部 | `org.springframework.context.*`、`org.springframework.jdbc.*` |

文件操作请用 `_safe_read_file` 等；网络请用 `_safe_http`；数据库请用 `_with_entity` DSL。

### 6.4 日志

```groovy
_log_component("Flow Start", "MyComponentName")
_print("debug info")
_print("metric", "csv", "perf_log", null)   // deviceType: console/db/csv
```

---

## 7. 动态实体 DSL 参考

所有写入宏使用完整 ACID 事务；只读宏禁用脏检查与刷盘，查询效率更高。

### 7.1 引导宏详解

引导宏是进入沙箱实体 DSL 的**唯一入口**。选错宏会导致：写入不提交、只读性能差、事务不生效、连接配额耗尽或 LOB 读取失败。

#### 7.1.1 宏一览

| 宏 | 事务类型 | 操作对象 | 典型场景 |
|----|----------|----------|----------|
| `_with_entity(arg)` | 读写 ACID | 动态实体业务数据 | 创建/更新/删除、含 LOB 的查询 |
| `_with_entity_by_id(id)` | 读写 ACID | 同上（按 FlexObject ID） | 与上行等价，语义更明确 |
| `_with_entity_by_name(name)` | 读写 ACID | 同上（按 FlexObject 名称） | 按显示名解析，先本 Territory 后 Global |
| `_with_entity_readonly(arg)` | 只读 | 动态实体业务数据 | loadField/loadData、列表、统计 |
| `_with_entity_by_id_readonly(id)` | 只读 | 同上 | 只读 + 按 ID |
| `_with_entity_by_name_readonly(name)` | 只读 | 同上 | 只读 + 按名称 |
| `_with_gorm_transaction { }` | GORM 事务 | **系统域**（FlexObject、Layout、Component 等） | 元数据维护、批量改配置 |
| `withTransaction { }`（闭包内） | Hibernate 子事务 | 当前 `_with_entity` 上下文内 | 同一实体库多步原子写入 |

**`_with_entity` 入参规则**：`Long` / `Integer` → 按 **FlexObject 主键 ID**；`String` → 按 **FlexObject code**（非 name）。

```groovy
_with_entity(294874L) { ... }      // 按 FlexObject.id
_with_entity('Order') { ... }      // 按 FlexObject.code
_with_entity_by_name('订单') { ... } // 按 FlexObject.name（先本租户，再 Global）
```

#### 7.1.2 写入宏 vs 只读宏

| 对比项 | 写入宏 `_with_entity*` | 只读宏 `_with_entity_readonly*` |
|--------|------------------------|----------------------------------|
| 底层事务 | `FlexObject.withTransaction`（完整 ACID） | `FlexObject.withTransaction([readOnly: true])` |
| `save` / `update` / `delete` | ✅ 有效并提交 | ❌ 不提交（脏检查与刷盘已关闭） |
| 查询性能 | 正常 | 约提升 40%（无脏检查） |
| LOB 字段（Geometry 等） | ✅ 有显式事务，可正常读 | ⚠️ PostgreSQL LOB 可能报 `auto-commit mode` 错误 |
| 适用面板 | createData、updateData、删除 | loadField、loadData、loadSearchField |

**选型口诀**：要改数据 → 写入宏；只查不改 → 只读宏。实体含 **Geometry / 大对象字段** 且只读查询报错时，改用 **写入宏** `_with_entity`（仍可不调用 save）。

```groovy
// ❌ 含 Geometry 字段时，只读宏可能触发 LOB 异常
_with_entity_readonly(336517L) {
    return gridData(parameters, [fixedQuery: [userId: user.id]])
}

// ✅ 改用写入宏（只查询、不 save，同样有事务上下文）
_with_entity(336517L) {
    return gridData(parameters, [fixedQuery: [userId: user.id]])
}
```

#### 7.1.3 禁止嵌套 `_with_entity`（及替代写法）

**规范：不要在组件中嵌套调用 `_with_entity` / `_with_entity_readonly`（内外层任意组合均避免）。**

原因：

1. **事务边界叠加**：每个引导宏外层都包裹 `FlexObject.withTransaction`，嵌套会产生多层事务，行为难预测。
2. **独立 Session 体系**：动态实体使用 `EntityDataSourceManager` 的 **独立 Hibernate Session**（与 Grails GORM 隔离），内外层宏各自管理 Session 生命周期，**不共享**同一实体上下文。
3. **连接配额**：每进入一层宏可能额外占用数据库连接（默认单次脚本上限 5 次），嵌套易触发 `SandboxLimitExceededException`。
4. **外层 `_with_gorm_transaction` 无法「兜住」内层 `_with_entity`**：二者属于不同事务体系，外裹 GORM 事务**不会**让内层动态实体的 JDBC 连接纳入同一事务（LOB 读取失败即源于此）。

```groovy
// ❌ 禁止：_with_entity 嵌套 _with_entity（即使不同 FlexObject）
_with_entity('Order') {
    _with_entity('OrderItem') {    // 错误
        ...
    }
}

// ❌ 禁止：_with_gorm_transaction 包裹 _with_entity（事务并不相通）
_with_gorm_transaction {
    _with_entity(294874L) { ... }  // 外层 GORM 事务对内层无效
}

// ❌ 禁止：只读嵌套写入，或写入嵌套只读
_with_entity_readonly('Order') {
    _with_entity('Order') { ... }
}
```

**正确替代方案**：

| 需求 | 做法 |
|------|------|
| 同一 FlexObject 内多步读写 | **单层** `_with_entity`，全程在同一闭包内完成 |
| 同一 FlexObject 内多步原子提交 | 单层 `_with_entity` + 闭包内 `withTransaction { }`（见 §7.1.6） |
| 跨 FlexObject 查关联业务数据 | 单层 `_with_entity` + `type('OtherCode')` / `find(clazz, ...)` / `get(type('X'), id)` |
| 操作 FlexObject / Layout / Component 等元数据 | 单独使用 `_with_gorm_transaction` + GORM 查询 + `_save_system_entity` |
| 先查动态数据、再改系统配置 | **分两段独立宏**（或两个组件），用 ID 传递，不要嵌套 |

```groovy
// ✅ 跨实体：单层 _with_entity + type()
_with_entity('Order') { clazz ->
    def items = find(type('OrderItem'), [orderId: parameters.objectId])
    def order = get(clazz, parameters.objectId as Long)
    ...
}

// ✅ 系统元数据：单独 _with_gorm_transaction
_with_gorm_transaction {
    def fo = com.next.FlexObject.get(parameters.id as Long)
    fo.active = false
    _save_system_entity(fo)
}
```

#### 7.1.4 `_with_entity` 与 `_with_gorm_transaction` 的区别

二者名称都含「事务」，但操作的 **数据层完全不同**：

| 对比项 | `_with_entity*` | `_with_gorm_transaction` |
|--------|-----------------|---------------------------|
| 数据对象 | FlexObject 对应的 **动态编译实体**（业务表数据） | Grails **系统域类**（`FlexObject`、`FlexObjectField`、`Layout`、`Component`、`User`…） |
| 事务引擎 | `FlexObject.withTransaction` + 独立 Hibernate Session | `Component.withTransaction`（标准 GORM） |
| 连接来源 | 业务库 HikariCP（`FlexObject.dataSource`） | 应用主库 GORM 数据源 |
| 闭包内 API | `find` / `get` / `save` / `bind` / `form` / `gridData` 等 EntityContext DSL | `com.next.FlexObject.where { }`、`new Layout(...)`、`_save_system_entity` |
| 典型用途 | 面板 CRUD、列表、表单 | 激活/停用对象、生成 Layout、清理字段、写 ComponentLog |
| 能否互相嵌套 | **否**（见 §7.1.3） | **否** |

```groovy
// 场景 A：用户提交订单表单 → _with_entity（业务数据）
{ parameters, user ->
    return _with_entity('Order') { clazz ->
        def params = _json_parse(parameters.object)
        def entity = create(clazz, [userId: user.id as Long])
        entity = bind(entity, params)
        save(entity)
        return [status: true, url: "..."]
    }
}

// 场景 B：停用 FlexObject 及其 Layout → _with_gorm_transaction（系统元数据）
{ parameters, user ->
    return _with_gorm_transaction {
        def target = com.next.FlexObject.get(parameters.id as Long)
        target.active = false
        _save_system_entity(target)

        com.next.Layout.where { name == "Index${target.code}" }.list().each {
            it.active = false
            _save_system_entity(it)
        }
        return "FlexObject usage has been suspended"
    }
}
```

**何时用哪个？**

- 操作 **业务表行**（订单、客户、附件记录等）→ `_with_entity*`
- 操作 **平台配置表**（FlexObject 定义、字段、布局、组件、菜单）→ `_with_gorm_transaction`
- 两者都需要时 → **分开两个宏、用 ID 串联**，不要嵌套

#### 7.1.5 闭包内 `withTransaction`（与 `_with_gorm_transaction` 不同）

`withTransaction { }` 是 **EntityContext 内部方法**，仅在已进入 `_with_entity*` 后可用，用于**同一动态实体库**内的多步原子操作：

```groovy
_with_entity('Order') {
    withTransaction {
        def order = create([userId: user.id as Long, code: 'O-01'])
        save(order)
        def item = create(type('OrderItem'), [orderId: order.id, name: 'Item A'])
        save(item)
    }  // 任一步失败，整体回滚
}
```

| 方法 | 作用域 | 回滚范围 |
|------|--------|----------|
| `withTransaction { }` | 当前 `_with_entity` 的 Hibernate Session | 该 Session 内操作 |
| `_with_gorm_transaction { }` | GORM 主库 | 系统域对象操作 |
| 整个 `_with_entity { }` 块 | 单次宏调用 | 宏结束时统一提交或回滚 |

单个 `save` 已自动提交时，仅多步需「全成功或全失败」才加 `withTransaction`。

#### 7.1.6 Territory 与权限

- `_with_entity_by_id(id)`：校验 FlexObject 须属于 **当前用户 Territory**（或 Global），否则 `Access denied`。
- `_with_entity('code')` / `_with_entity_by_name`：先查用户 Territory，再回退 Global。
- 行级数据权限：在 `_with_entity` 闭包内调用 `withUserFilter()`（见第 9 节），**默认不启用**。

#### 7.1.7 常见反模式速查

| 反模式 | 问题 | 正确做法 |
|--------|------|----------|
| `_with_entity` 嵌套 `_with_entity` | 事务/Session 混乱、连接耗尽 | 单层宏 + `type()` 跨实体 |
| `_with_gorm_transaction { _with_entity { } }` | 外层事务对内层无效 | 拆成独立两段 |
| 只读宏里 `save`/`update` | 不会提交 | 改用写入宏 |
| 含 LOB 字段用只读宏查列表 | PostgreSQL LOB 异常 | 改用 `_with_entity` |
| 在 `_with_gorm_transaction` 里 `create`/`bind` 业务实体 | 无 EntityContext | 改用 `_with_entity` |
| 循环中反复调用 `_with_entity` | 连接配额超限 | 合并为单次宏 + `batchSave` / `inList` 查询 |

#### 7.1.8 引导宏选型速查

```
要改业务表数据？          → _with_entity / _with_entity_by_id
只查业务表、不改？        → _with_entity_readonly（无 LOB 字段时）
含 Geometry/LOB 只查？   → _with_entity（写入宏，但不 save）
改 FlexObject/Layout 等？ → _with_gorm_transaction
同一业务库多步原子写？    → _with_entity + withTransaction { }
跨两个 FlexObject 业务表？ → 单层 _with_entity + type() / find(clazz,...)
```

---

### 7.2 查询方法

| 语法 | 说明 | 示例 |
|------|------|------|
| `find(query, opt)` | 匹配列表 | `find([status: 'A'], [sort:'id'])` |
| `findOne(query)` | 首条 | `findOne([code: 'O-01'])` |
| `find(clazz, query, opt)` | 跨实体 | `find(type('User'), [state: 1])` |
| `get(id)` | 按主键 | `get(101)` |
| `getByCode` / `getByName` / `getByField` | 唯一字段 | `getByCode('O-01')` |
| `count(query)` | 计数 | `count([status: 'A'])` |
| `page(query, opt)` | 分页 | `page([status:'A'], [page:1, pageSize:20])` |
| `typedFind` / `typedPage` | 带类型提示 | `typedFind([id:'1'], [id:'Long'])` |
| `query { }` | Criteria DSL | 见 7.4 节 |

### 7.3 写入方法

| 语法 | 说明 |
|------|------|
| `create(propertiesMap)` | 从 Map 创建 |
| `create(clazz, map)` | 跨实体创建 |
| `bind(entity, source)` | 智能属性绑定与类型转换 |
| `bind(entity, source, hints)` | 带类型提示绑定 |
| `save(entity)` / `update(entity)` | 持久化 |
| `updateFields(entity, map)` | 部分字段更新 |
| `delete(entity)` / `deleteById(id)` | 删除 |
| `batchSave(list)` | 批量插入 |
| `withTransaction { }` | 闭包内原子事务 |
| `withUserFilter()` | 行级安全过滤（见第 9 节） |

### 7.4 Query Builder（`query { }`）

`query { }` 是 `_with_entity*` 闭包内的高级 Criteria 查询 DSL，适合多条件组合、关联表路径、嵌套逻辑。入口在 `EntityContext`，返回 `SandboxQueryBuilder`，链式调用 `.list()` / `.get()` / `.count()` 执行。

```groovy
_with_entity('Order') { clazz ->
    def rows = query {
        eq "status", "Active"
        or {
            gt "amount", 100
            ilike "customer.name", "VIP"
            between "createdDate", startDate, endDate
        }
        order "id", "desc"
    }.list(max: 10)

    return toMapList(rows)   // 只读返回前脱管，见 §7.7
}
```

**执行方法**

| 方法 | 说明 | 示例 |
|------|------|------|
| `.list(options?)` | 返回列表 | `.list(max: 20, offset: 0, sort: 'id', order: 'desc')` |
| `.get()` | 返回单条（`maxResults=1`） | `query { eq 'code', 'O-01' }.get()` |
| `.count()` | 返回匹配总数 | `query { eq 'status', 'Active' }.count()` |

**通用说明**

- 支持点号路径关联查询，如 `"customer.name"`，自动创建 Hibernate alias。
- 同一 `query` 块内多条条件默认 **AND** 关系（全部满足）。
- `like` = **前缀匹配**（`MatchMode.START`）；`ilike` = **任意位置模糊、忽略大小写**（`MatchMode.ANYWHERE`）。
- `eq` 传入 `null` 等价于 `isNull`；`ne` 传入 `null` 等价于 `isNotNull`。
- `inList` 传入空集合时**不添加**该条件（避免生成无效 SQL）。
- `dateEq` 目前仅在 `query { }` **根级**可用；`and { }` / `or { }` 嵌套块内请改用 `between` 或根级 `dateEq`。

---

#### `eq` — 等于

```groovy
_with_entity('Order') {
    // 字符串 / 数值精确匹配
    def list = query {
        eq "status", "Active"
        eq "userId", user.id as Long
    }.list()

    // 关联字段
    def list2 = query {
        eq "customer.code", "C-001"
    }.list()

    // 传入 null 时自动转为 isNull
    def list3 = query {
        eq "deletedDate", null    // 等价于 isNull "deletedDate"
    }.list()
}
```

---

#### `ne` — 不等于

```groovy
_with_entity('Order') {
    def list = query {
        ne "status", "Cancelled"           // status <> 'Cancelled'
        ne "amount", 0
    }.list()

    // 传入 null 时自动转为 isNotNull
    def list2 = query {
        ne "remark", null                   // 等价于 isNotNull "remark"
    }.list()
}
```

---

#### `gt` / `ge` / `lt` / `le` — 大小比较

适用于数值、日期等可排序类型。

```groovy
_with_entity('Order') {
  def minAmount = 100
  def maxAmount = 5000
  def deadline = _parse_date("2026-12-31", "yyyy-MM-dd")

  def list = query {
      gt  "amount", minAmount              // amount > 100
      ge  "amount", minAmount              // amount >= 100
      lt  "amount", maxAmount              // amount < 5000
      le  "createdDate", deadline          // createdDate <= deadline
  }.list()
}
```

---

#### `like` — 前缀模糊匹配

**匹配规则**：`value%`（以 value 开头），区分大小写。

```groovy
_with_entity('Order') {
    def keyword = "ORD"

    def list = query {
        like "code", keyword               // code LIKE 'ORD%'
        like "customer.name", "张"          // 关联字段前缀匹配
    }.list()
}
```

---

#### `ilike` — 任意位置模糊匹配（忽略大小写）

**匹配规则**：`%value%`，不区分大小写。

```groovy
_with_entity('Order') {
    def keyword = "vip"

    def list = query {
        ilike "title", keyword             // title ILIKE '%vip%'
        ilike "customer.name", keyword
    }.list()
}
```

---

#### `inList` — 集合包含

```groovy
_with_entity('Order') {
    def allowedStatuses = ["Active", "Pending", "Approved"]
    def ids = [101L, 102L, 103L]

    def list = query {
        inList "status", allowedStatuses   // status IN (...)
        inList "id", ids
    }.list()

    // 空集合不会添加条件（安全跳过）
    def empty = []
    def list2 = query {
        inList "status", empty             // 被忽略，不报错
        eq "userId", user.id as Long
    }.list()
}
```

---

#### `isNull` / `isNotNull` — 空值判断

```groovy
_with_entity('Order') {
    def list = query {
        isNull     "deletedDate"           // 未删除
        isNull     "parentId"                // 顶级记录
        isNotNull  "approvedBy"            // 已审批
        isNotNull  "customer.name"         // 关联名称非空
    }.list()
}
```

---

#### `between` — 区间（闭区间）

适用于数值、日期时间范围。

```groovy
_with_entity('Order') {
    def startDate = _parse_date("2026-01-01", "yyyy-MM-dd")
    def endDate   = _parse_date("2026-01-31", "yyyy-MM-dd")

    def list = query {
        between "amount", 100, 9999
        between "createdDate", startDate, endDate
    }.list()
}
```

---

#### `dateEq` — 按「天」相等

将某日期字段匹配为**当天 00:00:00.000 ~ 23:59:59.999**，忽略时分秒。  
入参支持 `Date` 或 `"yyyy-MM-dd"` 字符串。

```groovy
_with_entity('Order') {
    // 字符串日期
    def list = query {
        dateEq "createdDate", "2026-06-25"
    }.list()

    // Date 对象
    def today = _parse_date(_today(), "yyyy-MM-dd")
    def list2 = query {
        dateEq "createdDate", today
    }.list()

    // 与其他条件组合
    def list3 = query {
        eq "status", "Active"
        dateEq "createdDate", parameters.searchDate   // 来自前端搜索参数
    }.list()
}
```

---

#### `and { }` — 显式 AND 分组

块内条件全部满足。根级多条条件本身已是 AND，`and { }` 用于**分组**或与 `or { }` 组合。

```groovy
_with_entity('Order') {
    def list = query {
        eq "userId", user.id as Long
        and {
            eq "status", "Active"
            gt "amount", 0
        }
    }.list()
}
```

---

#### `or { }` — 显式 OR 分组

块内条件满足**任意一条**即可。

```groovy
_with_entity('Order') {
    def list = query {
        eq "userId", user.id as Long
        or {
            eq  "status", "Active"
            eq  "status", "Pending"
            gt  "amount", 10000
        }
    }.list()
}
```

---

#### `and { }` + `or { }` 嵌套组合

```groovy
_with_entity('Order') {
    def list = query {
        // (status = 'Active' AND amount > 100) OR (status = 'VIP' AND amount > 500)
        or {
            and {
                eq "status", "Active"
                gt "amount", 100
            }
            and {
                eq "status", "VIP"
                gt "amount", 500
            }
        }
        isNull "deletedDate"
    }.list()
}
```

---

#### 综合示例：搜索 + 分页 + 计数

```groovy
{ parameters, user ->
    return _with_entity_readonly('Order') { clazz ->
        def searchMap = parameters.searchMap ?: [:]
        def keyword = searchMap.name?.toString()

        def builder = query {
            eq "userId", user.id as Long
            if (searchMap.status)
            {
                eq "status", searchMap.status
            }
            if (keyword)
            {
                ilike "name", keyword
            }
            if (searchMap.createdDate)
            {
                dateEq "createdDate", searchMap.createdDate
            }
            or {
                isNull "deletedDate"
                eq "status", "Archived"
            }
            order "id", "desc"
        }

        def total = builder.count()
        def rows  = builder.list(max: parameters.max ?: 20, offset: parameters.offset ?: 0)

        return [
            source: toMapList(rows),
            count : total
        ]
    }
}
```

---

#### 排序与分页

```groovy
_with_entity('Order') {
    // 方式 1：query 内 order
    def list = query {
        eq "status", "Active"
        order "createdDate", "desc"
        order "id", "asc"                  // 多字段排序：先 createdDate 再 id
    }.list(max: 10, offset: 0)

    // 方式 2：list 参数
    def list2 = query {
        eq "status", "Active"
    }.list(max: 10, offset: 20, sort: "id", order: "desc")
}
```

---

#### 与简单 `find` 的选择

| 场景 | 推荐 |
|------|------|
| 少量等值条件 | `find([status: 'A', userId: user.id])` |
| 模糊搜索、OR/AND 嵌套、关联路径 | `query { }` |
| 需要 `.count()` 与 `.list()` 共用条件 | `query { }` 先构建再分别调用 |

### 7.5 工具方法

| 方法 | 说明 |
|------|------|
| `delegateClass()` / `getDelegateClass()` | Team 授权实体 Class |
| `type(code)` | 跨实体加载 Class |
| `toMap(entity)` / `toMapList(list)` | 脱管为 Map（只读后校验/返回前使用，见 §7.7） |
| `detach(entity)` | 递归脱管，复杂关联场景替代 `toMap` |
| `convertValue(val, type)` | 类型强转 |
| `form` / `gridColumns` / `gridSearchSchema` / `gridData` | GUI 生成（见第 8 节） |

### 7.6 事务用法（补充）

单层 `_with_entity` 已自带 ACID 事务；仅当闭包内**多步写入必须原子提交**时，再叠加 `withTransaction`。  
与 `_with_gorm_transaction` 的区别、嵌套限制见 [§7.1.3](#713-禁止嵌套-_with_entity及替代写法)、[§7.1.4](#714-_with_entity-与-_with_gorm_transaction-的区别)、[§7.1.5](#715-闭包内-withtransaction与-_with_gorm_transaction-不同)。

```groovy
_with_entity('Order') {
    withTransaction {
        save(orderA)
        save(orderB)
    }  // 任一步失败，整体回滚
}
```

### 7.7 API 方法使用注意事项（实体生命周期）

实体 DSL 底层使用 Hibernate `Session`。`_with_entity*` / `_with_entity_readonly*` 闭包**结束时 Session 会关闭**，闭包外或跨请求持有的实体实例将变为 **detached（脱管）** 状态，不能再用于 `bind`、`update`、`delete`。

核心原则可以概括为两句话：

| 阶段 | 宏 | 原则 |
|------|-----|------|
| **只读** | `_with_entity_readonly*` | 查询后 **先 `toMap` / `toMapList` 脱管**，再做业务校验、过滤、组装 JSON |
| **写入** | `_with_entity*` | 在**同一次写入闭包内**用 `get(clazz, id)` **重新加载**，再 `bind` → `save` / `update` / `delete` |

#### 7.7.1 Session 边界示意

```
_with_entity_readonly(id) {
    def list = find(...)          ← 实体挂在当前 Session
    def maps = toMapList(list)    ← 转为普通 Map，可安全带出闭包
    return maps                   ← ✅ 可返回给前端
}                                 ← Session 关闭

_with_entity(id) {
    def entity = get(clazz, 101)  ← 必须在写入闭包内重新 get
    bind(entity, params)
    update(entity)                ← ✅ 同一 Session 内完成
}                                 ← 事务提交，Session 关闭
```

#### 7.7.2 只读阶段：先脱管，再校验

**适用场景**：`loadData`、自定义列表、导出前筛选、权限判断、组装画廊/统计等。

**正确做法**：

```groovy
{ parameters, user ->
    return _with_entity_readonly(294874L) { clazz ->
        def condition = [userId: user.id as Long]

        // 1. 查询
        def rawList = find(condition, [sort: 'id', order: 'asc'])

        // 2. 立刻脱管 —— 在访问字段、过滤、校验之前完成
        def rows = toMapList(rawList)

        // 3. 在 Map 上做业务逻辑（安全）
        def filtered = rows.findAll { row ->
            row.status == 'Active' && (row.amount as BigDecimal) > 0
        }

        return [list: filtered, count: filtered.size()]
    }
}
```

**为什么要先 `toMap`？**

- 脱管后的 Map 不依赖 Session，闭包结束后仍可安全使用。
- 避免在 `each` / `collect` 中触发懒加载关联（`entity.parent.name` 等）导致 `LazyInitializationException`。
- 避免 JSON 序列化时 Hibernate 代理对象无法序列化。
- 在 Map 上做校验、比较、拼接字符串更直观，且不意外修改持久化状态。

**`toMap` / `toMapList` 行为说明**：

- 关联对象默认只保留 **ID**，未初始化的集合会被跳过。
- 需要完整嵌套结构时，在只读闭包内用 `query` 一次取齐字段，或显式 `join`/字段投影，再 `toMap`。
- 复杂图结构可用 `detach(entity)`（递归脱管，带循环引用保护），效果类似但更彻底。

**反模式（不要这样写）**：

```groovy
// ❌ 直接返回托管实体列表
return find([status: 'A'])

// ❌ 未 toMap 就在循环里读可能懒加载的字段
find([:]).each { entity ->
    if (entity.customer?.name == 'VIP') { ... }  // 可能 LazyInitializationException
}

// ❌ 只读闭包查出实体，闭包外继续用
def cached
_with_entity_readonly(id) { cached = get(clazz, 101) }
_with_entity(id) { update(cached) }  // ❌ detached，跨 Session
```

**例外（可不手动 `toMap`）**：

| API | 说明 |
|-----|------|
| `form([id: ...])` | 内部加载实体并生成 schema，返回纯 Map |
| `gridData(parameters, options)` | 内部查询并转为表格行 Map |
| `gridColumns` / `gridSearchSchema` | 仅元数据，不涉及实体实例 |

自定义 `loadData`（如 t7 画廊）若不用 `gridData`，**必须**手动 `toMapList`。

#### 7.7.3 写入阶段：重新 `get`，禁止跨 Session 复用实体

**适用场景**：`createData`、`updateData`、删除、Team 授权、工作流写回等。

**更新标准模式**（与 t6 一致）：

```groovy
{ parameters, user ->
    return _with_entity(294874L) { clazz ->
        def params = _json_parse(parameters.object)
        def objectId = parameters.objectId?.toLong()

        params.remove('id')
        params.remove('userId')
        params.remove('parentId')

        // 写入闭包内重新 get —— 不要复用只读阶段或请求早期的实体
        def entity = get(clazz, objectId)
        if (!entity)
        {
            return [status: false, message: "Record not found: " + objectId]
        }

        entity = bind(entity, params)
        entity.userId = user.id as Long   // 系统字段由后端强制
        update(entity)

        return [status: true, url: "/flexObject/show/" + objectId + "?layoutCode=..."]
    }
}
```

**创建标准模式**（与 t4 一致）：

```groovy
// 创建没有“旧实体”可复用：create + bind + save 全程在同一写入闭包内
def entity = create(clazz, [userId: user.id as Long])
entity = bind(entity, params)
save(entity)
```

**删除前需要读字段时**（与 t8 一致）：

```groovy
// 可以在同一次写入闭包内 get → 读 parentId → deleteById
def entity = get(clazz, targetId)
def parentId = entity['parentId']
deleteById(targetId)
```

**Team 授权写入**（与 t13 一致）：

```groovy
// object 关联必须是当前 Session 内的托管实例
def mainEntity = get(clazz, targetObjectId)   // 重新 get 主记录
def teamEntity = create(delegateClazz, [userId: teamUserId, object: mainEntity])
save(teamEntity)
```

**反模式**：

```groovy
// ❌ 用只读宏做更新（只读事务内 save/update 不会提交）
_with_entity_readonly(id) {
    def e = get(clazz, 101)
    bind(e, params)
    update(e)   // 不会真正落库
}

// ❌ 把实体存到闭包外变量，下次请求或下一个 _with_entity 再用
def staleEntity = null
_with_entity(id) { staleEntity = get(clazz, 101) }
// ... 其他逻辑 ...
_with_entity(id) { update(staleEntity) }   // ❌

// ❌ 前端传来的“实体对象”直接当托管实体用
def entity = params.entity   // 只是 Map，不是 Session 托管对象
update(entity)
```

#### 7.7.4 同一脚本内的「先查后写」

若必须在一次组件执行中先查询再写入，使用**写入宏**包裹全程，或分两段但第二段必须 `get`：

```groovy
_with_entity('Order') { clazz ->
    // 阶段 A：查询（仍在写入 Session 内，可读可写）
    def existing = findOne([code: params.code])
    if (existing)
    {
        return [status: false, message: 'Code already exists']
    }

    // 阶段 B：创建（同一 Session，无需 toMap 再 get）
    def entity = create(clazz, [userId: user.id as Long])
    entity = bind(entity, params)
    save(entity)
}
```

若阶段 A 仅只读且数据量大，可拆为 `_with_entity_readonly` 查出 **ID 或 Map**，写入时再 `_with_entity` + `get(clazz, id)`。**不要把只读阶段的实体实例直接传给写入阶段。**

#### 7.7.5 常用 API 对照

| API | 只读/写入 | 注意事项 |
|-----|-----------|----------|
| `find` / `findOne` / `page` | 通常只读 | 返回托管实体；给前端前 `toMapList` / `toMap` |
| `get(clazz, id)` | 写入前必用 | 更新/删除/授权前在**写入闭包内**调用 |
| `create` | 写入 | 返回新托管实例，同一闭包内 `bind` + `save` |
| `bind(entity, params)` | 写入 | `entity` 必须是当前 Session 的托管对象 |
| `save` / `update` | 写入 | 仅 `_with_entity*`（或 `withTransaction`）内有效 |
| `updateFields` | 写入 | 先 `get` 再部分更新，适合少数字段 |
| `delete` / `deleteById` | 写入 | `deleteById` 更简洁；需读字段时先 `get` |
| `batchSave` | 写入 | 列表内实体均须本次 `create` 生成，勿混入 detached 实例 |
| `withUserFilter()` | 读/写 | 写入时影响归属校验；更新/删除前建议启用 |
| `toMap` / `toMapList` | 只读后处理 | 校验、过滤、返回 JSON 前使用 |
| `detach` | 只读后处理 | 复杂关联树的脱管替代方案 |
| `gridData` | 只读 | 已内置转 Map，一般无需再 `toMap` |
| `form` | 只读 | 传 `id` 时内部加载；返回 schema Map |

#### 7.7.6 与面板生命周期的对应关系

| 面板操作 | 组件类型 | 宏选择 | 实体处理要点 |
|----------|----------|--------|--------------|
| `loadField` / `loadSearchField` | 只读 | `_with_entity_readonly` | `form` / `gridColumns` / `gridSearchSchema` 即可 |
| `loadData` | 只读 | `_with_entity_readonly` | 优先 `gridData`；自定义逻辑用 `toMapList` |
| `createData` | 写入 | `_with_entity` | `create` + `bind` + `save`，无旧实体 |
| `updateData` | 写入 | `_with_entity` | `get(clazz, objectId)` → `bind` → `update` |

`createData` 与 `updateData` 是**两次独立 HTTP 请求**，各自开启新的 Session；不存在“上一次 load 的实体留给本次 update”的情况——每次写入都必须按 `objectId` 重新 `get`。

#### 7.7.7 异步与跨线程

`evaluateAsync` 在**独立线程**执行，没有 HTTP 请求的 Hibernate Session：

- 不要在异步组件里依赖调用方预加载的 GORM 实体。
- 异步路径应在脚本内重新 `_with_entity` 并用 ID 查询。
- 只传递 ID、Map、String 等可序列化数据，不传递实体实例。

---

## 8. GUI 生成方法详解

### 8.1 form(options) — 表单 Schema

返回 `{schema, form}` 供前端 SchemaForm 渲染。

| 参数 | 说明 |
|------|------|
| `contextKey` | `"onCreate"` / `"onEdit"` / `"onView"`，按字段元数据过滤 |
| `id` | 实体 ID，编辑/查看时回填数据 |
| `readonly` | `true` 时只读并隐藏提交按钮 |
| `submitTitle` | 提交按钮文字 |
| `fieldCss` | 默认栅格，如 `"col-lg-4"` |
| `fields` / `excludes` | 白名单 / 黑名单字段 |
| `defaults` | `[字段code: 默认值]` |
| `css` | `[字段code: "col-lg-12"]` 单字段样式 |
| `layout` | 自定义布局数组，传入后完全覆盖默认排版 |

**新建页**：无 `id`，`contextKey: "onCreate"`，`readonly: false`  
**编辑页**：带 `id`，`contextKey: "onEdit"`  
**查看页**：带 `id`，`readonly: true`，`contextKey: "onView"`

> 注意：请使用 `fieldCss`，历史字段名 `searchFieldCss` 已不再生效。  
> `form` 在传入 `id` 时会在**内部**加载实体并生成 schema，调用方无需手动 `get` / `toMap`。

### 8.2 gridColumns(options) — 表格列

| 参数 | 说明 |
|------|------|
| `showUrl` | 行点击跳转前缀，如 `"/flexObject/show/"` |
| `layoutCode` | 详情页布局编号 |
| `columns` | 自定义列 `[[header:"Name", field:"name"], ...]` |
| `buttonFields` | 渲染为按钮的字段 |
| `newPage` | `true` 新窗口打开 |
| `reload` / `reloadTime` | 定时刷新 |

### 8.3 gridSearchSchema(options) — 搜索表单

| 参数 | 说明 |
|------|------|
| `searchButtonTitle` | 搜索按钮文字 |
| `searchFieldCss` | 搜索框栅格，如 `"col-lg-3"` |
| `searchFields` | 强制显示的搜索字段白名单 |
| `defaults` | 搜索框默认值 |

### 8.4 gridData(parameters, options) — 表格数据

| 参数 | 说明 |
|------|------|
| `fixedQuery` | **安全围栏**，后端强制过滤，前端无法覆盖 |
| `max` / `pageSize` | 分页条数 |
| `orderBy` / `orderDir` | 强制排序（默认 `id desc`） |
| `searchFields` | 允许前端动态搜索的字段白名单 |
| `aliasPrefix` | Criteria 别名前缀（Team 模式常用 `"object"`） |
| `additionalCriteria` | 自定义 Criteria 闭包 |
| `columns` | 仅返回指定列，减少载荷 |

跨实体重载：`gridData(clazz, parameters, options)`

`parameters` 常用字段：`page`、`objectId`、`searchMap`（前端搜索条件）。

> `gridData` 内部已完成查询与 **Map 化**，返回的 `source` 可直接给前端，无需再 `toMapList`。安全过滤务必配置 `fixedQuery`（见 §7.7.2 例外说明）。

---

## 9. Team 团队协作模式

当 `FlexObject.team = true` 时启用。系统自动生成：

- **主实体**：`FO_<code>`（表名通常为 `<code>`）
- **授权表**：`Team_<code>`（表名 `team_<code>`），字段：`userId`、`createdDate`、`object`（指向主实体）

### 9.1 判断与获取

```groovy
_with_entity('Order') {
    if (flexObject?.team) {
        def dc = delegateClass()  // 或 getDelegateClass()
    }
}
```

### 9.2 可见数据查询（必须 withUserFilter）

```groovy
_with_entity('Order') {
    withUserFilter()
    return page([status: 'Active'], [page: 1, pageSize: 20, sort: 'id', order: 'desc'])
}
```

- **非 team**：自动追加 `userId = 当前用户`
- **team**：合并「自己创建的」+「被授权共享的」数据

Team 模式 `page()` 先在内存合并再分页，建议加过滤条件控制数据量。

### 9.3 写入/更新/删除权限

| 操作 | 权限 |
|------|------|
| `create/save` | `withUserFilter()` 下 `userId` 为空时自动填充当前用户 |
| `update/updateFields` | 创建者 **或** 被授权用户 |
| `delete/deleteById` | **仅创建者**（授权不放宽删除） |

### 9.4 查询授权表

```groovy
_with_entity('Order') {
    def dc = delegateClass()
    return find(dc, ['object.id': targetObjectId], [sort: 'id', order: 'asc'])
}
```

Team 列表用 `gridData` + `aliasPrefix: "object"`：

```groovy
gridData(delegateClazz, parameters, [
    aliasPrefix: 'object',
    fixedQuery : [userId: user.id]
])
```

---

## 10. 沙箱内置 API 速查

所有函数在脚本 Binding 中直接可用，均受资源配额约束。

### 10.1 附件

| 函数 | 说明 |
|------|------|
| `_upload_attachments_file(content, fileName)` | 上传 Base64/流，限 10MB |
| `_download_attachments_file_base64(att)` | 下载 Base64，限 10MB |
| `_download_attachments_file_stream(target)` | 下载流（注意关闭） |
| `_get_attachment_file_url(fileName)` | 临时公开 URL |
| `_save_attachment` / `_delete_attachment` / `_delete_attachment_file` | 附件 CRUD |

### 10.2 日期时间

| 函数 | 说明 |
|------|------|
| `_now(zoneId?)` | `ZonedDateTime` |
| `_now_millis()` | 时间戳 |
| `_today()` | `yyyy-MM-dd` |
| `_date_format(dt, pattern)` | 格式化 |
| `_format_now(pattern)` | 格式化当前时间 |
| `_parse_date(str, pattern)` | 解析为 Date |
| `_parse_zoned_date_time(str, pattern)` | 解析为 ZonedDateTime |
| `_date_add(dt, amount, unit)` | 加减（days/months/seconds 等） |

### 10.3 JSON

| 函数 | 说明 |
|------|------|
| `_json_parse(json)` | 反序列化 |
| `_json_stringify(obj)` | 序列化 |
| `_json_pretty(obj)` | 美化输出 |

### 10.4 加密

| 函数 | 说明 |
|------|------|
| `_encrypt_md5` / `_encrypt_sha256` / `_encrypt_sm3` | 哈希 |
| `_generate_aes_key` / `_encrypt_aes` / `_decrypt_aes` | AES |
| `_generate_sm4_key` / `_encrypt_sm4` / `_decrypt_sm4` | 国密 SM4 |
| `_generate_sm2_keys` / `_encrypt_sm2` / `_decrypt_sm2` | 国密 SM2 |
| `_generate_tink_keyset` / `_encrypt_tink` / `_decrypt_tink` | Google Tink |

### 10.5 工作流

入参均为 `WorkflowInstance`，返回 `[flag: Boolean, message: String]`：

| 函数 | 说明 |
|------|------|
| `_workflow_approve(instance)` | 审批通过 |
| `_workflow_reject(instance)` | 拒绝打回 |
| `_workflow_reset(instance)` | 重置到初始 |
| `_workflow_cancel(instance)` | 取消终止 |
| `_workflow_complete(instance)` | 超管完结 |
| `_workflow_init(instance)` | 激活新实例 |

### 10.6 内网 API

| 函数 | 说明 |
|------|------|
| `_api(accountExternalId, interfaceCode, parameters)` | 调用内网注册 API |

### 10.7 HTTP（白名单 URL）

| 函数 | 说明 |
|------|------|
| `_safe_http(url)` | 同步 HttpBuilder |
| `_safe_http_async(url)` | 异步 HttpBuilder |
| `_safe_http_get_async` / `_safe_http_post_async` | 带回调的异步请求 |
| `_safe_read_url_text(url)` | 读取文本 |
| `_safe_read_url_bytes` / `_safe_read_url_stream` | 读取二进制 |
| `_base64_encode` / `_base64_decode` | Base64 |
| `_url_encode` / `_url_decode` | URL 编解码 |

### 10.8 文件（白名单路径）

路径限定在 `/app/data/`、`/app/uploads/` 等配置区域，单文件默认 10MB。

| 函数 | 说明 |
|------|------|
| `_safe_list_files` / `_safe_mkdirs` / `_safe_file_exists` | 目录操作 |
| `_safe_read_file` / `_safe_write_file` | 文本读写 |
| `_safe_read_file_bytes` / `_safe_write_file_bytes` | 二进制读写 |
| `_safe_read_file_stream` / `_safe_write_file_stream` | 流读写 |

### 10.9 表格文件

| 函数 | 说明 |
|------|------|
| `_parse_excel(stream)` / `_parse_csv(string)` | 解析为 `List<Map>` |
| `_parse_excel_file(path)` / `_parse_csv_file(path)` | 从沙箱路径解析 |
| `_create_excel(data)` / `_create_csv(data)` | 生成内存数据 |
| `_create_excel_file` / `_create_csv_file` | 写入沙箱路径 |

### 10.10 邮件与短信

| 函数 | 说明 |
|------|------|
| `_send_email(to, subject, body)` | 纯文本邮件 |
| `_send_html_email(to, subject, htmlBody)` | HTML 邮件 |
| `_send_sms(phone, signName, tplCode, params)` | 短信（默认供应商） |
| `_send_sms_via(provider, ...)` | 指定供应商 |

### 10.11 AI

| 函数 | 说明 |
|------|------|
| `_ask_ai(prompt)` | 阻塞提问（默认 provider） |
| `_ask_ai_via(provider, prompt, options)` | 指定 provider |
| `_ask_ai_async` / `_ask_ai_via_async` | 异步 + 回调 |

### 10.12 文档转换

| 函数 | 说明 |
|------|------|
| `_render_markdown(md)` | MD → HTML |
| `_word_to_html(base64)` / `_word_to_markdown(base64)` | Word 转换 |
| `_word_to_pdf(base64)` | Word → PDF |
| `_generate_pdf(text)` | 文本 → PDF |
| `_generate_pdf_from_html(html)` | HTML → PDF |

### 10.13 系统实体

| 函数 | 说明 |
|------|------|
| `_find_or_create_system_entity(Class, query, init)` | 系统域 CRUD |
| `_save_system_entity(entity)` | 保存系统实体 |

---

## 11. 系统配置与执行限额

配置位于 `application.yml` → `ksanatalk` 命名空间。

### 11.1 脚本执行

| 配置键 | 默认值 | 说明 |
|--------|--------|------|
| `scriptExecution.componentMaxExecutionTime` | 60000 ms | 组件脚本超时 |
| `scriptExecution.buttonMaxExecutionTime` | 3000 ms | 按钮脚本超时 |
| `scriptExecution.scriptMaxSize` | 100000 | 脚本+依赖最大字符数 |
| `scriptExecution.maxRunningScripts` | 80 | 全局最大并发脚本 |
| `scriptExecution.maxMemoryUsage` | 2048 MB | 堆内存熔断阈值 |
| `scriptExecution.cacheMaxSize` | 50000 | 编译缓存条目 |
| `scriptExecution.classLoaderRecycleThreshold` | 500 | 触发 ClassLoader 回收的编译次数 |
| `scriptExecution.territoryMaxDepth` | 20 | Territory 继承查找深度 |

### 11.2 单次资源配额

| 配置键 | 默认值 |
|--------|--------|
| `sandbox.limit.databaseConnectionLimit` | 5 |
| `sandbox.limit.httpRequestLimit` | 5 |
| `sandbox.limit.fileOperationLimit` | 20 |
| `sandbox.limit.emailRequestLimit` | 5 |
| `sandbox.limit.smsRequestLimit` | 5 |
| `sandbox.limit.aiRequestLimit` | 5 |

### 11.3 沙箱安全路径

| 配置键 | 说明 |
|--------|------|
| `sandbox.allowedFilePaths` | 允许读写的文件路径前缀 |
| `sandbox.allowedNetworkUrls` | 允许访问的 URL 前缀 |
| `sandbox.allowedUrlSchemes` | 允许的协议（http/https） |
| `sandbox.maxFileSizeBytes` | 单文件上限（默认 10MB） |
| `scriptSecurity.disallowedClasses` | 编译期禁止的类/包列表 |

---

## 12. 故障排查

| 异常 / 现象 | 原因 | 处理 |
|-------------|------|------|
| `SandboxLimitExceededException: database connection limit` | 循环或嵌套中过多 `_with_entity` | 合并为单层宏；勿嵌套引导宏（§7.1.3） |
| `Large Objects may not be used in auto-commit mode` | 含 Geometry/LOB 字段使用只读宏 | 改用 `_with_entity` 写入宏（§7.1.2） |
| `_with_gorm_transaction` 内 `_with_entity` 事务不生效 | 两套事务体系隔离 | 拆成独立两段，勿嵌套（§7.1.3） |
| `SandboxLimitExceededException: HTTP request limit` | 循环中过多 HTTP | 合并请求或优化逻辑 |
| `SecurityException: access to class java.io.File is disallowed` | 使用了禁止类 | 改用 `_safe_*` 沙箱函数 |
| `ScriptExecutionException: script is empty` | 组件 script 为空 | 检查数据库记录 |
| `ScriptExecutionException: Component script too large` | 超过 scriptMaxSize | 拆分为 Library 组件 |
| `LazyInitializationException` | 在 Session 外访问实体或懒加载字段 | 只读：`toMapList` 后再处理；勿返回托管实体 |
| `LazyInitializationException`（更新时） | 对 detached 实体 `bind`/`update` | 写入闭包内 `get(clazz, objectId)` 重新加载 |
| 只读组件里 `update` 不生效 | 使用了 `_with_entity_readonly` | 改用 `_with_entity` |
| `Metaspace OOM` | 动态编译类过多 | 降低 `classLoaderRecycleThreshold` |
| `SecurityException: Invalid sort field name` | 非法排序字段 | 前端 sort 参数被安全拦截（预期行为） |
| `IllegalArgumentException: Cannot convert 'abc' to Integer` | 绑定类型错误 | 检查前端提交数据格式 |
| 异步事件无效果 | 异常被吞掉 | 查 `ComponentLog` 与应用 ERROR 日志 |
| 按钮不显示 | 条件脚本返回 false 或超时 | 检查 `{ id, user ->` 逻辑 |

**审计日志**：搜索 `SCRIPT_AUDIT`，包含 user、component、hash、duration、SYNC/ASYNC 模式。

---

## 13. 功能示例（t1–t16）

以下 16 个示例覆盖面板开发的全部标准场景。  
将 `${thisFlexObject?.id}` 替换为你的 FlexObject ID，`${indexLayout.code}` 等替换为实际布局 code。

### 13.1 t1 — 新建表单 Schema（loadField / onCreate）

**场景**：DataCreatePanel 加载字段，渲染可编辑新建表单。

```groovy
{ parameters, user ->
    return _with_entity(${thisFlexObject?.id}) {
        return form([// === 核心数据与上下文 ===
                     contextKey : "onCreate", // 字段过滤上下文标识，如 "onCreate", "onEdit", "onView"，会根据 FlexObjectField 的复选框配置来过滤字段
                     // id         : parameters.objectId ? (parameters.objectId as Long) : null, // 实体数据ID：用于“编辑/查看”时自动查询并回显该实体的数据

                     // === 全局状态控制 ===
                     readonly   : false, // 是否将整个表单强制渲染为只读模式 (设为 true 时会自动隐藏提交按钮)
                     submitTitle: "Save", // 底部提交按钮显示的文本

                     // === 布局与样式配置 ===
                     fieldCss   : "col-lg-4", // 表单字段默认的栅格宽度 CSS (如 col-lg-4 为三列布局)
                     // col        : 3, // 可选：附加到返回结果中的元数据 (通常告诉前端按几列渲染)
                     // layout     : [], // 可选：自定义的 SchemaForm 布局数组 (会完全覆盖默认生成的由上到下表单排版)

                     // === 字段级微调 (按需解开注释) ===
                     // fields     : ["code1", "code2"], // 白名单过滤：仅渲染数组中指定的字段 code
                     // excludes   : ["code3"], // 黑名单过滤：不渲染数组中指定的字段 code
                     // defaults   : [status: "New"], // 默认值覆盖：格式为 [字段code: 默认值]
                     // css        : [name: "col-lg-12"] // 单独字段样式覆盖：格式为 [字段code: "CSS样式"]
        ])
    }
}
```

### 13.2 t2 — 创建数据（含附件上传）

**场景**：createData，解析 JSON、上传 Base64 附件、保存实体、返回跳转 URL。

```groovy
{ parameters, user ->
    return _with_entity(${thisFlexObject?.id}) { clazz ->
        // 1. 解析参数
        def params = _json_parse(parameters.object)

        // 2. 附件处理
        params.fileUrl?.each { file ->
            def startIndex = (file.base64String as String).indexOf(',') + 1
            def encodedData = (file.base64String as String).substring(startIndex)
            def ossFileName = (_now_millis as String) + (file.name as String)
            def attachments = _upload_attachments_file(encodedData, ossFileName)
            params[file.key] = attachments?.fileUrl?.toString()
        }

        // 3. 防御性清理：防止用户恶意覆盖系统保留字段
        params.remove("id")
        params.remove("userId")
        params.remove("parentId")

        // 4. 构造默认/强制的基础属性
        def baseFields = [userId: user.id as Long]
        def hasParent = flexObject?.parent?.name != null
        if (hasParent && parameters.objectId)
        {
            baseFields.parentId = parameters.objectId as Long
        }

        // 5. 创建实例并使用 Sandbox EntityDataBinder 自动完成类型转换和映射绑定
        def entity = create(clazz, baseFields)
        entity = bind(entity, params)
        save(entity)

        // 6. 返回跳转链接
        if (hasParent)
        {
            return ["status": true, "url": "/flexObject/show/" + parameters.objectId + "?layoutCode=${indexLayout.code}"]
        }
        else
        {
            return ["status": true, "url": "/flexObject/index?layoutCode=${indexLayout.code}"]
        }
    }
}
```

### 13.3 t3 — 批量附件上传（每条记录一个文件）

**场景**：AttachmentsUploadPanel，多文件各建一条子表记录。

```groovy
{ parameters, user ->
    return _with_entity(${thisFlexObject?.id}) { clazz ->
        def params = _json_parse(parameters.object)

        if (!params.fileUrl)
        {
            return ["status": false, "message": "Please select the file to upload first！"]
        }

        params.remove("id")
        params.remove("userId")
        params.remove("parentId")

        def baseFields = [userId: user.id as Long]
        def hasParent = flexObject?.parent?.name != null
        if (hasParent && parameters.objectId)
        {
            baseFields.parentId = parameters.objectId as Long
        }

        def attachmentField = com.next.FlexObjectField.where { flexObject.id == ${thisFlexObject?.id} && active == true && dataType == "Attachment" }.get()?.name

        def entitiesToSave = []

        params.fileUrl?.each { file ->
            def entity = create(clazz, baseFields)
            entity = bind(entity, params)

            if (attachmentField)
            {
                def startIndex = (file.base64String as String).indexOf(',') + 1
                def encodedData = (file.base64String as String).substring(startIndex)
                def ossFileName = (_now_millis as String) + (file.name as String)
                def attachments = _upload_attachments_file(encodedData, ossFileName)
                entity[attachmentField] = attachments?.fileUrl?.toString()
            }

            entitiesToSave << entity
        }

        batchSave(entitiesToSave)

        if (hasParent)
        {
            return ["status": true, "url": "/flexObject/show/" + parameters.objectId + "?layoutCode=${indexLayout.code}"]
        }
        else
        {
            return ["status": true, "url": "/flexObject/index?layoutCode=${indexLayout.code}"]
        }
    }
}
```

### 13.4 t4 — 标准创建（无附件）

```groovy
{ parameters, user ->
    return _with_entity(${thisFlexObject?.id}) { clazz ->
        def params = _json_parse(parameters.object)

        params.remove("id")
        params.remove("userId")
        params.remove("parentId")

        def baseFields = [userId: user.id as Long]
        if (flexObject?.parent?.name && parameters.objectId)
        {
            baseFields.parentId = parameters.objectId as Long
        }

        def entity = create(clazz, baseFields)
        entity = bind(entity, params)
        save(entity)

        return ["status": true, "url": "/flexObject/show/" + entity.id?.toString() + "?layoutCode=${showLayout.code}"]
    }
}
```

### 13.5 t5 — 查看表单 Schema（onView / 只读）

```groovy
{ parameters, user ->
    return _with_entity(${thisFlexObject?.id}) {
        return form([
                     contextKey: "onView",
                     id        : parameters.objectId ? (parameters.objectId as Long) : null,
                     readonly  : true,
                     fieldCss  : "col-lg-4",
        ])
    }
}
```

### 13.6 t6 — 更新数据（updateData）

```groovy
{ parameters, user ->
    return _with_entity(${thisFlexObject?.id}) { clazz ->
        def params = _json_parse(parameters.object)

        def entityId = parameters.objectId?.toLong()
        def entity = get(clazz, entityId)
        if (!entity)
        {
            return ["status": false, "message": "Record not found: " + entityId]
        }

        params.remove("id")
        params.remove("userId")
        params.remove("parentId")

        entity = bind(entity, params)
        entity.userId = user.id as Long
        update(entity)

        return ["status": true, "url": "/flexObject/show/" + parameters.objectId + "?layoutCode=${showLayout.code}"]
    }
}
```

### 13.7 t7 — 图片画廊数据（LightGallery）

```groovy
{ parameters, user ->
    return _with_entity_readonly(${thisFlexObject?.id}) { clazz ->
        def condition = [userId: user.id as Long]

        if (flexObject?.parent?.name && parameters.objectId)
        {
            condition.parentId = parameters.objectId as Long
        }

        def entityList = toMapList(find(condition, [sort: "id", order: "asc"]))

        def schema = [:]
        def form = []
        def list = []
        def imageSuffixRegex = ~/.*\.(?i)(jpg|jpeg|png|gif|bmp)$/

        def attachmentField = com.next.FlexObjectField.where { flexObject.id == ${thisFlexObject?.id} && active == true && dataType == "Attachment" }.get()?.name

        if (attachmentField)
        {
            entityList.each { rawMap ->
                def attachmentVal = rawMap[attachmentField.name]?.toString()
                if (attachmentVal && attachmentVal ==~ imageSuffixRegex)
                {
                    def source = [id: rawMap.id]

                    fields.each { field ->
                        if (field.dataType == "Attachment")
                        {
                            source["originalUrl"] = rawMap[field.name]
                        }
                        if (field.description?.contains("title"))
                        {
                            source["title"] = rawMap[field.name] ?: ""
                        }
                        if (field.description?.contains("description"))
                        {
                            source["description"] = rawMap[field.name] ?: ""
                        }
                        source[field.name] = rawMap[field.name]
                    }
                    list << source
                }
            }
        }

        return [data: ["schema": schema, "form": form, "params": ["fieldHtmlClass": "grid-input-none-style"]], list: list, canDelete: '${thisFlexObject?.canDelete}']
    }
}
```

### 13.8 t8 — 删除数据

```groovy
{ parameters, user ->
    return _with_entity(${thisFlexObject?.id}) { clazz ->
        def targetId = parameters.id?.toLong()

        if (!targetId)
        {
            return ["status": false, "message": "Missing target id to delete."]
        }

        def entity = get(clazz, targetId)
        if (!entity)
        {
            return ["status": false, "message": "Record not found: " + targetId]
        }

        def parentId = entity['parentId']
        def hasParent = flexObject?.parent?.name != null

        deleteById(targetId)

        if (hasParent && parentId)
        {
            return ["targetUrl": "/flexObject/show/" + parentId + "?layoutCode=${indexLayout.code}"]
        }
        else
        {
            return ["targetUrl": "/flexObject/index?layoutCode=${indexLayout.code}"]
        }
    }
}
```

### 13.9 t9 — 表格列定义（gridColumns）

```groovy
{ parameters, user ->
    return _with_entity_readonly(${thisFlexObject?.id}) {
        return gridColumns([
                            showUrl   : "/flexObject/show/",
                            layoutCode: "${showLayout.code}",
        ])
    }
}
```

### 13.10 t10 — 表格数据加载（gridData / 普通模式）

```groovy
{ parameters, user ->
    return _with_entity_readonly(${thisFlexObject?.id}) { clazz ->

        def fixedQuery = [userId: user.id as Long]

        if (flexObject?.parent?.name && parameters.objectId)
        {
            fixedQuery["parentId"] = parameters.objectId as Long
        }

        int page = parameters.page ? parameters.page as int : 1
        int maxRecords = page > 1 ? 200 : 201

        return gridData(parameters, [
                                     fixedQuery: fixedQuery,
                                     max       : maxRecords,
        ])
    }
}
```

### 13.11 t11 — 搜索表单 Schema（gridSearchSchema）

```groovy
{ parameters, user ->
    return _with_entity_readonly(${thisFlexObject?.id}) {
        return gridSearchSchema([
                                 searchButtonTitle: "Search",
                                 searchFieldCss   : "col-lg-3",
        ])
    }
}
```

### 13.12 t12 — 表格数据（Team 团队模式）

```groovy
{ parameters, user ->
    return _with_entity_readonly(${thisFlexObject?.id}) { clazz ->
        def fixedQuery = [:]
        def options = [:]

        int page = parameters.page ? parameters.page as int : 1
        options.max = page > 1 ? 200 : 201

        def delegateClazz
        if (flexObject?.parent?.name)
        {
            fixedQuery["parentId"] = parameters.objectId as Long
            delegateClazz = clazz
        }
        else
        {
            fixedQuery["userId"] = user.id as Long
            options.aliasPrefix = "object"
            delegateClazz = delegateClass()
        }

        options.fixedQuery = fixedQuery

        return gridData(delegateClazz, parameters, options)
    }
}
```

### 13.13 t13 — Team 授权（添加协作成员）

```groovy
{ parameters, user ->
    return _with_entity(${thisFlexObject?.id}) { clazz ->
        def targetObjectId = parameters?.objectId?.toLong()

        if (!targetObjectId)
        {
            return ["status": false, "message": "Missing objectId."]
        }

        def entity = get(clazz, targetObjectId)
        if (!entity)
        {
            return ["status": false, "message": "Target object not found: " + targetObjectId]
        }

        def delegateClazz = getDelegateClass()
        if (!delegateClazz)
        {
            return ["status": false, "message": "This object does not have Team mode enabled (No delegate class found)."]
        }

        def teamFields = [userId: params.teamUser as Long, object: entity]

        def teamEntity = create(delegateClazz, teamFields)
        save(teamEntity)

        return ["status": true, "url": "/flexObject/show/" + targetObjectId + "?layoutCode=${layout.code}"]
    }
}
```

### 13.14 t14 — Team 成员列表

```groovy
{ parameters, user ->
    return _with_entity_readonly(${thisFlexObject?.id}) { clazz ->

        def delegateClazz = getDelegateClass()
        if (!delegateClazz)
        {
            return ["status": false, "message": "This object does not have Team mode enabled."]
        }

        def targetObjectId = parameters.objectId as Long
        if (!targetObjectId)
        {
            return ["status": false, "message": "Missing objectId parameter."]
        }

        def entityList = find(delegateClazz, ["object.id": targetObjectId], [sort: "id", order: "asc"])

        def sources = []

        if (entityList)
        {
            def userIds = entityList.collect { it['userId'] as Long }.unique()
            def userMap = [:]
            if (userIds)
            {
                com.next.User.where { id in userIds }.projections {
                    property('id')
                    property('fullName')
                }.list().each { row -> userMap[row[0] as Long] = row[1] }
            }

            entityList.each { entity ->
                def source = [:]
                source.id = entity['id']
                source.user = userMap[entity['userId'] as Long] ?: "Unknown User"
                source.createdDate = entity['createdDate'] ? _date_format(entity['createdDate'], 'yyyy-MM-dd HH:mm:ss') : ""
                sources << source
            }
        }

        return [source   : sources,
                paging   : ["enabled": true, "page": 1, "pageSize": 10, "display": 1],
                searching: ["enabled": true, "placeholder": 'Please enter the content you want to search for', "highlighting": true],
                count    : entityList.size()]
    }
}
```

### 13.15 t15 — Team 授权详情（只读表单）

```groovy
{ parameters, user ->
    return _with_entity_readonly(${thisFlexObject?.id}) { clazz ->
        def delegateClazz = getDelegateClass()
        if (!delegateClazz)
        {
            return ["status": false, "message": "This object does not have Team mode enabled."]
        }

        def targetObjectId = parameters.objectId as Long
        if (!targetObjectId)
        {
            return ["status": false, "message": "Missing objectId parameter."]
        }

        def entity = get(delegateClazz, targetObjectId)
        if (!entity)
        {
            return ["status": false, "message": "Team assignment record not found: " + targetObjectId]
        }

        def teamUser = com.next.User.where { id == (entity['userId'] as Long) }.get()

        def schema = [:]
        def form = []

        schema["fullName"] = ["type": "string", "title": "Full Name", "default": teamUser?.fullName ?: ""]
        form << ["key": "fullName", "htmlClass": "col-lg-4"]

        schema["username"] = ["type": "string", "title": "User Name", "default": teamUser?.username ?: ""]
        form << ["key": "username", "htmlClass": "col-lg-4"]

        schema["createdDate"] = ["type": "string", "title": "Created Date", "default": entity['createdDate'] ? _date_format(entity['createdDate'], 'yyyy-MM-dd HH:mm:ss') : ""]
        form << ["key": "createdDate", "htmlClass": "col-lg-4"]

        return ["schema": schema, "form": form, "params": ["fieldHtmlClass": "grid-input-none-style"]]
    }
}
```

### 13.16 t16 — 删除 Team 授权记录

```groovy
{ parameters, user ->
    return _with_entity(${thisFlexObject?.id}) { clazz ->
        def delegateClazz = getDelegateClass()
        if (!delegateClazz)
        {
            return ["status": false, "message": "This object does not have Team mode enabled."]
        }

        def targetId = parameters.id?.toLong()
        if (!targetId)
        {
            return ["status": false, "message": "Missing target id to delete."]
        }

        def entity = get(delegateClazz, targetId)
        if (!entity)
        {
            return ["status": false, "message": "Team assignment record not found: " + targetId]
        }

        def objectId = entity['object']?.id

        delete(entity)

        return ["targetUrl": "/flexObject/show/" + objectId + "?layoutCode=${layout.code}"]
    }
}
```

---

## 14. 面板与组件的对应关系

| Panel 操作 | HTTP 端点 | Panel 字段 | 典型示例 |
|------------|-----------|------------|----------|
| 加载表单/列 | `loadField` | `fieldComponent` | t1, t5, t9 |
| 加载搜索 | `loadSearchField` | `searchFieldComponent` | t11 |
| 加载数据 | `loadData` | `dataComponent` | t7, t10, t12, t14 |
| 创建 | `createData` | `createComponent` | t2, t3, t4 |
| 更新 | `updateData` | `updateComponent` | t6 |

按钮点击执行组件：通过 `BaseController` 按组件 `name` 查找并 `evaluate`，传入 `[id: objectId, objectId: objectId]`。

---

## 15. 按钮条件脚本

控制按钮是否显示，**签名不同**：

```groovy
{ id, user ->
    return user?.username == 'admin'   // 必须返回 Boolean
}
```

支持 **import 引用**（从第 2 行起连续 import 行，遇到非 import 行停止）：

```groovy
{ id, user ->
import my_helper_lib
    return someHelperFunction(id, user)
}
```

`import` 行会被替换为同 Territory 下同名组件的 `script` 正文。超时默认 3 秒。

---

## 16. 工作流组件

签名 `{ parameters, user ->`，但 `parameters` 实际是 `WorkflowInstance`：

```groovy
{ instance, user ->
    def result = _workflow_approve(instance)
    return result   // [flag: Boolean, message: String]
}
```

| 模式 | 行为 |
|------|------|
| 同步事件 | `evaluate()` 阻塞，结果写入 `WorkflowInstanceEvent.logs` |
| 异步事件 | `evaluateAsync()` 火忘，错误记入 `ComponentLog` |

---

## 17. 快速对照：我该写哪种组件？

| 需求 | 用什么 | 读写宏 |
|------|--------|--------|
| 生成新建/编辑/查看表单 | `form([...])` | `_with_entity_readonly` |
| 生成表格列 | `gridColumns([...])` | `_with_entity_readonly` |
| 生成搜索框 | `gridSearchSchema([...])` | `_with_entity_readonly` |
| 加载表格行数据 | `gridData(parameters, options)` | `_with_entity_readonly` |
| 新增/修改/删除记录 | `create` / `bind` / `save` / `update` / `delete` | `_with_entity`（写入前 `get` 重新加载） |
| 只读查询后返回 JSON | `toMapList(find(...))` | `_with_entity_readonly` |
| 操作 FlexObject/Layout 等系统配置 | `_with_gorm_transaction` + GORM + `_save_system_entity` | 勿与 `_with_entity` 嵌套 |
| 跨 FlexObject 业务表查询 | `type('Code')` / `find(clazz, ...)` | 单层 `_with_entity` |
| 纯自定义 JSON | 自己组装 Map/List | 视情况，或不用引导宏 |
| 调外部 HTTP | `_safe_http` / `_safe_read_url_text` | 不需要 entity |
| 发邮件/短信 | `_send_email` / `_send_sms` | 不需要 entity |
| 工作流审批 | `_workflow_approve(instance)` 等 | 可选 |

---

## 18. 检查清单（提交前自检）

- [ ] 闭包签名正确：`{ parameters, user ->` 或按钮 `{ id, user ->`
- [ ] 实体操作包裹在 `_with_entity*` 中（系统域操作用 `_with_gorm_transaction`）
- [ ] **未嵌套** `_with_entity` / `_with_gorm_transaction`（§7.1.3）
- [ ] 只读场景使用 `_with_entity_readonly`（含 LOB 字段时改用写入宏）
- [ ] **只读**：`find`/`page` 结果已 `toMapList`，再在 Map 上校验/过滤/返回
- [ ] **写入**：更新/删除前在同一 `_with_entity` 内 `get(clazz, objectId)`，未复用跨闭包实体
- [ ] 创建/更新前 `params.remove("id"/"userId"/"parentId")`
- [ ] 查询有 `fixedQuery` 或 `withUserFilter()` 围栏
- [ ] 未在 `_with_entity_readonly` 内调用 `save`/`update`/`delete`
- [ ] 未使用禁用的 Java 类（File、Runtime、java.net 等）
- [ ] 脚本 + 依赖总大小 < `scriptMaxSize`（100000）
- [ ] 失败路径返回 `[status: false, message: "..."]`
- [ ] 成功跳转返回 `url` 或 `targetUrl`

---

## 19. 最小可运行示例

不依赖 FlexObject，用于验证环境：

```groovy
{ parameters, user ->
    return [
        status : true,
        message: "Hello, ${user?.fullName ?: user?.username}",
        time   : _format_now("yyyy-MM-dd HH:mm:ss")
    ]
}
```

将脚本保存为组件（`active = true`），通过面板绑定或组件执行入口测试。

---

*本文档涵盖 KsanaTalk v9.0.0 组件开发的全部规范。如有平台版本差异，以实际部署环境的 `application.yml` 配置为准。*
