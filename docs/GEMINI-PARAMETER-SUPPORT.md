# Gemini 模型参数真实性矩阵

- generated_at: 2026-07-11T13:23:58Z
- endpoint: https://fast.aieyra.cn/v1
- model_count: 21
- `accepted_unverified` 表示上游接受请求且本地确认参数已发送，不代表已证明采样效果。
- `model_unavailable` 表示模型或渠道当前不可用，不能据此判断参数支持。

| 模型 | temperature | top_p | top_k | max_tokens | json_output |
|---|---|---|---|---|---|
| gemini-2.5-flash | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified |
| gemini-2.5-flash-image | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified |
| gemini-2.5-flash-thinking | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified |
| gemini-2.5-pro | model_unavailable | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified |
| gemini-2.5-pro-thinking | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified |
| gemini-3-flash | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified |
| gemini-3-flash-agent | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified |
| gemini-3-flash-preview | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified |
| gemini-3-pro-image-preview | accepted_unverified | accepted_unverified | accepted_unverified | request_error | accepted_unverified |
| gemini-3-pro-preview | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified |
| gemini-3-pro-preview-thinking | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified |
| gemini-3.1-flash-image | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified |
| gemini-3.1-flash-image-preview | accepted_unverified | accepted_unverified | request_error | accepted_unverified | accepted_unverified |
| gemini-3.1-flash-lite | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified |
| gemini-3.1-pro-high | model_unavailable | model_unavailable | model_unavailable | model_unavailable | model_unavailable |
| gemini-3.1-pro-low | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified |
| gemini-3.1-pro-preview | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified |
| gemini-3.1-pro-preview-thinking | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified |
| gemini-3.5-flash-extra-low | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified |
| gemini-3.5-flash-low | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified |
| gemini-pro-agent | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified | accepted_unverified |

## 判定规则

- `accepted_unverified`: 参数字段已进入真实请求，上游返回成功，但无法仅凭单次响应证明效果。
- `accepted_model_mismatch`: 请求成功，但响应模型与请求模型不一致。
- `rejected`: 上游明确拒绝参数或请求体。
- `model_unavailable`: 503、circuit breaker、无可用 token、404 或无渠道。
- `local_not_sent`: ProjectLing 本地没有把设置写入请求体，属于本地缺陷。
- 详细 request/response model、耗时、usage 与脱敏错误见 JSON。
