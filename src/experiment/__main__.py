"""`python -m src.experiment` 的入口。**没有逻辑** —— 全在 `driver.main()`。

留这么薄是有意的：`main()` 收 `argv` 并返回退出码，所以它能被普通单测调用
（不用 `subprocess` 起一个自己）；而这个文件只有一行 `sys.exit`。
"""

import sys

from .driver import main

if __name__ == "__main__":
    sys.exit(main())
