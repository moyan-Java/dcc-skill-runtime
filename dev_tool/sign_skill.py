"""为本地 Skill 包生成 Ed25519 签名，不把私钥提交到仓库。"""

from __future__ import annotations

import argparse
from pathlib import Path

from dcc_skill_runtime.security import SkillSigner


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="sign_skill")
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--key-id", required=True)
    args = parser.parse_args(argv)
    print(SkillSigner.sign_package(args.package, args.private_key, args.key_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
