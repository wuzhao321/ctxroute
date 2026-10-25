"""Save an API key from hidden terminal input without command-line arguments."""

import getpass
import sys
import warnings
from pathlib import Path

from ctxroute.secrets import save_new_key


def main() -> None:
    if not sys.stdin.isatty():
        raise SystemExit("请在交互式终端运行；不接受管道或命令行参数中的密钥。")
    root = Path(__file__).resolve().parents[1]
    if (root / ".env").exists():
        raise SystemExit(".env 已存在，未覆盖。需要更换密钥时请编辑该文件。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            key = getpass.getpass("粘贴 DeepSeek API key（输入隐藏），然后按回车：")
    except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
        raise SystemExit("已取消：无法保证隐藏输入，未保存密钥。") from None
    try:
        save_new_key(root, key)
    except (OSError, ValueError):
        raise SystemExit("未能保存密钥；请检查文件权限或输入格式。") from None
    print("已保存到项目 .env，权限 600；没有执行 API 调用。")


if __name__ == "__main__":
    main()
