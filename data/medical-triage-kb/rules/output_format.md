# Agent 标准输出格式

Agent 必须输出合法 JSON，结构固定如下。

## 字段定义

- `primary_department`：首选推荐科室（字符串）。
- `alternative_departments`：备选科室列表（字符串数组，可为空数组）。
- `confidence`：推荐置信度，取值 `high` / `medium` / `low`。
- `reason`：推荐理由，用简洁中文描述匹配到的症状要点。
- `red_flag_warning`：急诊提示。无急诊风险时仍要给出"未发现明确急诊信号，如症状加重请及时就医"类提示。
- `follow_up_questions`：澄清问题列表，2-5 条。
- `disclaimer`：固定免责声明。

## JSON 示例

```json
{
  "primary_department": "消化内科",
  "alternative_departments": ["普外科", "急诊科"],
  "confidence": "medium",
  "reason": "用户主要描述腹痛、腹泻、食欲下降，优先考虑胃肠道相关问题，适合先到消化内科就诊。",
  "red_flag_warning": "如果腹痛剧烈、持续加重，或伴高热、频繁呕吐、便血、黑便，应及时线下急诊就医。",
  "follow_up_questions": [
    "腹痛具体在什么位置？",
    "有没有发烧、呕吐、便血或黑便？",
    "症状持续多久了？"
  ],
  "disclaimer": "以上仅为就诊科室建议，不能替代医生诊断。"
}
```

## 输出约束

1. 不得在 `reason` 中下确诊结论（如"你就是胃炎"）。
2. 不得提供具体药物、剂量、处方。
3. `primary_department` 命中急诊危险信号时必须为 `急诊科`。
4. `disclaimer` 必须始终包含，建议固定为："以上仅为就诊科室建议，不能替代医生诊断。"
5. 输出必须是合法 JSON，不要附加 Markdown 代码块外的额外解释。
