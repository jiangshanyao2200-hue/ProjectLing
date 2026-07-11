# ProjectLing Relay Model Compatibility

- generated_at: 2026-07-11T13:46:49Z
- endpoint: https://fast.aieyra.cn/v1
- model_count: 23
- recommended: 6
- usable_limited: 10
- diagnostic_only: 5
- incompatible_or_unavailable: 2
- luna_match_count: 0
- exact_5_6_compact_exposed: False

## Matrix

| Model | Category | Text | SSE | Tool | Thinking | Verdict | ProjectLing |
|---|---|---:|---:|---:|---:|---|---|
| claude-opus-4-6-thinking | claude | yes | yes | yes | accepted/no-trace | usable_limited | limited |
| claude-sonnet-4-6 | claude | yes | yes | yes | - | usable_limited | limited |
| gemini-2.5-flash | flash | yes | no | yes | - | usable_limited | limited |
| gemini-2.5-flash-image | image | no | no | no | - | incompatible | unsupported |
| gemini-2.5-flash-thinking | thinking | yes | yes | yes | yes | usable_limited | limited |
| gemini-2.5-pro | pro | yes | yes | yes | - | recommended | stable |
| gemini-2.5-pro-thinking | thinking | yes | yes | yes | yes | usable_limited | limited |
| gemini-3-flash | flash | yes | yes | yes | - | recommended | stable |
| gemini-3-flash-agent | agent | yes | yes | yes | - | diagnostic_only | unsupported |
| gemini-3-flash-preview | flash | yes | yes | yes | - | usable_limited | stable |
| gemini-3-pro-image-preview | image | yes | no | yes | - | diagnostic_only | unsupported |
| gemini-3-pro-preview | pro | yes | yes | no | - | usable_limited | planner_only |
| gemini-3-pro-preview-thinking | thinking | yes | yes | yes | yes | usable_limited | limited |
| gemini-3.1-flash-image | image | yes | yes | yes | - | diagnostic_only | unsupported |
| gemini-3.1-flash-image-preview | image | no | no | yes | - | diagnostic_only | unsupported |
| gemini-3.1-flash-lite | lite | yes | yes | yes | - | recommended | stable |
| gemini-3.1-pro-high | pro | no | no | no | - | unavailable | unsupported |
| gemini-3.1-pro-low | pro | yes | yes | yes | - | recommended | stable |
| gemini-3.1-pro-preview | pro | yes | yes | no | - | usable_limited | planner_only |
| gemini-3.1-pro-preview-thinking | thinking | yes | yes | yes | yes | usable_limited | limited |
| gemini-3.5-flash-extra-low | flash | yes | yes | yes | - | recommended | stable |
| gemini-3.5-flash-low | flash | yes | yes | yes | - | recommended | stable |
| gemini-pro-agent | agent | yes | yes | no | - | diagnostic_only | unsupported |

## Channel Findings

- `luna`: server metadata match count is 0; no exposed model contains that name.
- exact `5.6 compact`: exposed=False.
- Related but not exposed to this token: `gpt-5.6-terra`, `gpt-5.6-sol`, `gpt-5.5-openai-compact`, `gpt-5.4-openai-compact`
- Image and agent variants are not recommended as ProjectLing main/executor chat models even when a basic compatibility probe returns data.
- Detailed latencies, response shapes, usage, and sanitized errors are in the JSON artifact.
