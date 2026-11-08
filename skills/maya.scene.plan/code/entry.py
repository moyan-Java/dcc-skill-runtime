"""Maya 场景计划 Skill 的按需入口。"""


def run(_context, args):
    """复用插件已有的场景校验和事务应用实现。"""

    if not isinstance(args, dict):
        raise ValueError("Skill 参数必须是对象")
    mode = args.get("mode", "validate")
    plan = args.get("plan")
    if mode not in ("validate", "apply"):
        raise ValueError("mode 必须是 validate 或 apply")
    if not isinstance(plan, dict):
        raise ValueError("plan 必须是对象")

    from dcc_maya_plugin import scene_builder

    if mode == "validate":
        return scene_builder.validate_wall_plan(plan)
    return scene_builder._apply_on_main_thread(plan)
