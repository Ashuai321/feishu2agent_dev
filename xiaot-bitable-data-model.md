# 小 T 多维表格数据范围与字段语义（DEV）

仅用于 Agent「小T多维表格管理_DEV」。数据范围固定为 Base `CNd6bKd5ZaRWkssv5rUccCmBn0e`，不得据此访问其他 Base、表或外部数据源。

| 表 | Table ID | 用途 |
|---|---|---|
| goal | `tblHq7aqhe195HnD` | 高层战略目标或分类，用于组织相关项目，不直接表示具体执行。 |
| project | `tblGljHRF28Jb3W3` | Goal 下相对完整的事项或阶段目标。 |
| task | `tblWvG10yZtqpp2A` | 具体执行单元，描述负责人、时间和待办工作。 |
| sub_task | `tblRSAd7V63Zp2ya` | Task 的细分步骤或 checklist。 |
| space_list | `tblyhk92pJBej7Vs` | 可供管理和引用的空间清单。 |
| changelog | `tbl5GmADqShvAx6I` | 记录操作者、时间及 goal/project/task/sub_task 等对象的变更。 |
| ai_text | `tbl24pWCd60PP7GJ` | 每日任务分析文本。分类选项为「个人消息分析文本」和「群分析文本」，不可互换。 |

## 状态业务含义

Goal 与 Project：`Daily` 表示日常、无具体内容；`Later` 表示细节已明确但低优先级暂缓；`Waiting/Blocked` 表示依赖或问题导致受阻；`Ongoing` 表示低优先级推进；`Important` 表示高优先级推进；`Finished` 表示完成。

Task 与 SubTask：`Todo` 表示待开始；`In Progress` 表示正在执行；`Later` 表示明确但延后；`Waiting/Blocked` 表示依赖、信息或条件不足；`Reviewing` 表示执行完成、待审阅或确认；`Finished` 表示完成并确认。

业务状态名称不保证与表格下拉选项一致。每次写状态前都要读取目标字段的实时选项并原样使用：Goal/Project 当前 `Normal` 对应 `Ongoing`；Task/SubTask 当前 `To-do` 对应 `Todo`。缺少目标状态选项时先向用户确认，不新增选项。
