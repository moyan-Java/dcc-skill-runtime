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

Runtime 不在自己的 Python 进程执行 Maya API。它只把已校验的 Skill 请求发送给
Host Service，再由 Maya 插件按请求动态加载 Skill。

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
