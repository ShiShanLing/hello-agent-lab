---
name: goal-planning
description: 区分制定新计划、查看已有计划和单条 Todo。用户提到计划、目标拆解、步骤或 /制定计划 时使用。
---

# 计划与 Todo 的分工

1. 询问已有计划、进度或步骤时，使用 `list_todo_plans`；看某一个计划用 `get_todo_plan`。
2. 旅行计划和 Todo 计划不是同一类数据。问旅行行程时改用 `list_travel_plans` / `get_travel_plan`。
3. 用户只是要记一件事时，用 `add_todo`，不要升级成完整计划。
4. 把大目标拆成可执行步骤时，说明应使用「制定计划」流程；聊天工具不能直接生成结构化计划卡片。
5. 不要把尚未保存的步骤说成已经写入 Todo。
