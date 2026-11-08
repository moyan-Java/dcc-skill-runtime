# Maya 场景计划

这个 Skill 复用 Maya 插件已有的场景计划校验和事务应用逻辑。

输入参数：

```json
{
  "mode": "validate",
  "plan": {
    "version": 1,
    "name": "Hero_Asset",
    "nodes": []
  }
}
```

`mode=validate` 只校验，不修改场景；`mode=apply` 会在用户确认后创建节点。
Skill 入口只在收到 `skill.run` 时加载，Maya 启动时不会导入它。
