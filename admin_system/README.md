# Medical Agent 管理后台

`admin_system` 是 `Medical_Agent` 的独立管理后台，用于管理 PostgreSQL 结构化数据、用户上传源文件和 Chroma 向量库。

## 功能

- 首页总览：用户、报告、会话、消息、预约、向量数量。
- 可视化图表：问诊场景分布、报告类型分布、近 14 天新增趋势。
- 用户管理：查询用户、查看用户详情、查看预约/报告/问诊记录、删除用户。
- 报告管理：查询报告、查看 AI 解析摘要、单个/批量删除报告。
- 问诊记录：查询会话、查看消息、删除单个或批量删除会话。
- 知识库管理：上传知识库、删除单个知识库文件及其向量、重建知识库向量、清空知识库向量。
- 向量管理：清理用户报告向量。

## 启动

在项目根目录执行：

```powershell
cd <PROJECT_ROOT>
$env:MEDIX_ADMIN_USERNAME="admin"
$env:MEDIX_ADMIN_PASSWORD="<YOUR_ADMIN_PASSWORD>"
python -m uvicorn admin_system.backend.main:app --host 127.0.0.1 --port 8022 --reload
```

访问：

```text
http://127.0.0.1:8022/
```

登录：

```text
账号：MEDIX_ADMIN_USERNAME
密码：MEDIX_ADMIN_PASSWORD
```

请通过环境变量设置后台账号密码，例如 `<ADMIN_USERNAME> / <ADMIN_PASSWORD>`。正式部署不要使用弱密码。

## 数据删除规则

删除报告会同步删除：

1. 上传源文件。
2. Chroma `user_reports` 中对应 `user_id/doc_id` 的向量。
3. PostgreSQL `message_attachments` 关联。
4. PostgreSQL `medical_documents` 记录。

删除用户并级联时会同步删除：

- 用户报告源文件、报告记录、报告向量。
- 用户会话、消息、问诊记录。
- 预约记录和健康档案。

## 知识库规则

上传知识库文件会保存到：

```text
data/knowledge_base/admin_uploads/
```

并立即写入 Chroma `medical_kb` collection。

清空知识库向量只影响 `medical_kb`，不会删除用户报告向量。
