---
name: todo-hygiene
description: 规范地添加、查看和完成待办。用户提到任务、Todo、待办、完成任务或 /添加任务 时使用。
---

# Todo 操作规范

1. 用户要求记录任务时，必须调用 `add_todo`，不要只在回复里假装已保存。
2. 查看任务时调用 `list_todos`；不要靠聊天记忆猜测当前列表。
3. 完成任务前如果不知道 ID，先 `list_todos`。修改状态只能用 `complete_todo`。
4. `complete_todo` 需要用户确认。工具返回取消时，明确说操作未执行。
5. 一条消息只处理用户明确提出的任务，不要主动批量创建无关 Todo。
