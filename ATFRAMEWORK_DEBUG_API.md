# ATFramework Debug API

## 概要

这个接口用于 Session Viewer 的多行调试。

前端当前按顺序逐行调用 `POST /runteststeps`。

- 当前步骤拿到明确终态结果后，才会发送下一步。
- 当前步骤失败后，后续步骤停止。
- 当前执行行会高亮，结束后标记成功或失败。

当前前端只支持方案 A，也就是后端同步返回当前步骤终态。

## 请求

- 地址: `POST /runteststeps`
- Content-Type: `application/json`
- 调用方式: 单步顺序发送，不发送数组

请求体示例：

```json
{
  "clientStepId": "debug-step-15",
  "ControlName": "LoginButton",
  "Action": "Click",
  "ParameterValue": "{\"imagePath\":\"Y:\\\\Recorder\\\\recordings\\\\session_xxx\\\\step_12.png\"}",
  "Check": "Null",
  "CheckParameterValue": "",
  "StepDescription": "点击登录按钮",
  "Expectresult": "进入首页"
}
```

说明：

- `clientStepId` 格式固定为 `debug-step-{行号}`，例如 `debug-step-15`。
- 前端不会发送 `rowIndex`。
- `ParameterValue` / `CheckParameterValue` 中如果有带 `path` 的相对路径，前端会在发送前转成绝对路径。
- `clientStepId` 建议后端原样回传，用于前后端日志对齐。

## 后端返回协议

### 方案 A: 同步终态返回

后端在 `POST /runteststeps` 返回时，这一步必须已经真实执行结束。

成功示例：

```json
{
  "completed": true,
  "success": true,
  "clientStepId": "debug-step-15",
  "message": "点击成功"
}
```

失败示例：

```json
{
  "completed": true,
  "success": false,
  "clientStepId": "debug-step-15",
  "message": "元素不存在"
}
```

必须满足：

- `completed=true`
- `success=true/false`
- 建议返回 `message`
- 建议原样回传 `clientStepId`

## 前端当前可识别字段

- `clientStepId`
- `completed`
- `success`
- `message` / `msg` / `detail` / `result`

前端认为步骤完成的条件是：

- `completed=true`
- 同时存在 `success=true/false`

## 非法返回

如果后端只是返回普通 `200 OK`，但：

- 没有 `completed=true`
- 或没有 `success=true/false`

前端会判定接口协议不匹配，并停止调试，不会继续发送下一步。

## 推荐实现

直接按方案 A 实现即可：

- 原样回传 `clientStepId`
- 提供清晰的 `message`
- 失败时返回可直接展示给用户的错误原因

## 联调检查清单

1. 多选 3 行后，前端只会先发第 1 行。
2. 第 1 行返回终态前，后端不会收到第 2 行。
3. 第 1 行成功后，前端才会发送第 2 行。
4. 第 2 行失败后，前端停止，不再发送第 3 行。
5. 后端返回的 `message` 能在 Viewer 状态栏中看到。
6. 当前执行行会高亮，完成后会标记成功或失败。