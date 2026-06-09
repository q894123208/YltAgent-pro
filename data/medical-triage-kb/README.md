# 智能互联网医院分诊知识库

## 1. 知识库用途

本知识库用于"智能互联网医院分诊 Agent"的 RAG 检索，根据用户朴素、口语化的症状描述，向用户推荐合适的医院就诊科室。

- 仅用于"科室分诊建议"。
- 不替代医生诊断。
- 不直接给出疾病确诊结论。
- 不提供具体处方、药物名称、剂量或治疗方案。

## 2. 目录结构

```
medical-triage-kb/
├── README.md                       本说明文件
├── departments/                    26 个科室的分诊知识
├── rules/
│   ├── red_flags.md                统一急诊危险信号规则
│   ├── department_conflict_rules.md 多科室冲突判断规则
│   └── output_format.md            Agent 标准输出格式
└── metadata/
    ├── department_aliases.json     科室别名映射
    └── department_priority.json    科室推荐优先级规则
```

## 3. RAG 检索建议

- 按"科室文件"做主切片，每个科室一个文档。
- 在每个文件内可对小节（口语化描述、症状关键词、示例问答）进一步细分切片。
- 用户输入先做关键词命中和语义相似度检索。
- 命中多个科室时，参考 `rules/department_conflict_rules.md` 与 `metadata/department_priority.json`。
- 任何输入都需要先经过 `rules/red_flags.md` 急诊规则过滤。

## 4. 推荐输出格式

输出格式见 `rules/output_format.md`，字段包括：

- primary_department
- alternative_departments
- confidence
- reason
- red_flag_warning
- follow_up_questions
- disclaimer

## 5. 医疗安全免责声明

- 本知识库的内容仅供分诊参考。
- 输出建议不构成诊断、处方或医疗决策。
- 用户最终应以线下医生面诊结论为准。
- 在出现急诊危险信号时，应立即建议用户拨打 120 或前往最近医院急诊。

## 6. 使用限制

不建议把本知识库用于：

- 疾病确诊与排他诊断。
- 处方药、剂量、用药方案推荐。
- 急救现场操作指导（应直接呼叫 120）。
- 替代专业心理危机干预。
