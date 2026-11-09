# DCC Skill Runtime

独立的 Skill 编排运行时。它负责 Skill 元数据索引、按需读取、会话记录、
知识检索和 Host Service 调用，不导入 Maya 或其他 DCC API。

## 当前边界

- Python `>=3.10,<3.11`；
- Skill 启动时只读取 `skill.yaml`；
- 选中 Skill 后才读取 `SKILL.md`、引用和入口信息；
- DCC 执行继续通过现有 JSON/WebSocket `host.call`；
- Skill 包使用 Ed25519 签名；
- 知识库使用 SQLite FTS，支持可配置的 OpenAI-compatible Embedding；
- 向量服务不可用时降级为关键词检索。

默认目录：

```text
%LOCALAPPDATA%\DccHost\skills
%LOCALAPPDATA%\DccHost\runtime\session.db
%LOCALAPPDATA%\DccHost\knowledge\index.db
%LOCALAPPDATA%\DccHost\config\skill-trust.json
```

## Skill 包

```text
maya-scene-plan/
├── skill.yaml
├── SKILL.md
├── code/entry.py
├── references/
└── signature.json
```

Skill 入口约定为：

```python
def run(context, args):
    ...
```

Skill 可以在 `skill.yaml` 中声明顶层参数。Runtime 和 Maya 插件都会在执行前检查必填字段、基础类型、枚举值和未声明字段：

```json
{
  "args": {
    "properties": {
      "mode": {"type": "string", "enum": ["validate", "apply"]},
      "plan": {"type": "object"}
    },
    "required": ["plan"],
    "additionalProperties": false
  }
}
```

这只是 Skill 输入的边界校验，不执行任意 JSON Schema，也不会替代 Skill 自己的业务校验。

Runtime 不在自己的 Python 进程执行 Maya API。它只把已校验的 Skill 请求发送给
Host Service，再由 Maya 插件按请求动态加载 Skill。

## MCP 工具

Runtime 使用本地 Streamable HTTP 提供固定工具。Agent 通常先调用
MCP 只向 Agent 暴露三个稳定入口：

- `skill_list`：查看已安装 Skill 的元数据；
- `host_list`：查看当前在线的 DCC 宿主；
- `skill_run`：提交一个 Skill 任务并返回 `task_id`。

会话、排队、运行、超时、取消和结果状态由 Runtime 的任务表维护，不要求 Agent 轮询状态。
Skill 的详细说明仍通过 `skill://{skill_id}` 只读资源读取；任务状态查询和取消保留为
Runtime 内部能力，后续再根据实际需要提供管理入口。

## 开发签名

开发者不要把私钥提交到仓库。可以使用 `SkillSigner` 生成本地私钥、信任配置和
Skill 签名：

```python
from pathlib import Path
from dcc_skill_runtime.security import SkillSigner

SkillSigner.generate_key_pair(
    Path("private.key"),
    Path("skill-trust.json"),
    "dev",
)
SkillSigner.sign_package(
    Path("skills/maya.scene.plan"),
    Path("private.key"),
    "dev",
)
```

`private.key` 只保存在发布环境；Maya 插件和 Runtime 只需要信任配置中的公钥。

仓库内提供了一个示例 Skill：`skills/maya.scene.plan`。签名后可以把它安装到
`%LOCALAPPDATA%\DccHost\skills`，它会复用 Maya 插件已有的受限计划校验和事务应用逻辑。
示例 `skill.yaml` 使用 YAML 兼容的 JSON 写法，保证 Maya 插件侧不必额外安装 YAML 解析库。
