"""创建独立后台管理员的服务器命令。"""

import argparse
from getpass import getpass
import os
from pathlib import Path

from hello_agent.admin_auth import SqliteAdminAuthService
from hello_agent.app import DEFAULT_DATABASE_FILE


def main() -> None:
    parser = argparse.ArgumentParser(description="创建独立后台管理员账号")
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="系统管理员")
    parser.add_argument(
        "--knowledge-manager",
        action="store_true",
        help="允许该后台管理员上传、删除和导入公共知识库资料",
    )
    arguments = parser.parse_args()
    first = getpass("设置临时后台密码（至少 12 位）：")
    second = getpass("再次输入临时后台密码：")
    if first != second:
        raise SystemExit("两次输入的密码不一致。")
    database_file = Path(
        os.getenv("TODO_DATABASE_FILE", str(DEFAULT_DATABASE_FILE))
    )
    try:
        admin = SqliteAdminAuthService(database_file).bootstrap(
            arguments.email,
            first,
            arguments.name,
            can_manage_knowledge=arguments.knowledge_manager,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(f"已创建后台管理员：{admin.email}")
    print("首次登录必须修改密码并绑定 Authenticator。")


if __name__ == "__main__":
    main()
