# app/utils/cli.py

# 1 导入依赖
from textwrap import dedent


# 2 居中输出工具
def center_cli_str(text: str, width: int | None = None):
    """
    将多行字符串在终端中居中显示。
    """
    import shutil

    width = width or shutil.get_terminal_size().columns
    lines = text.split("\n")
    max_line_len = max(len(line) for line in lines)
    return "\n".join((line + " " * (max_line_len - len(line))).center(width) for line in lines)


# 3 ASCII Banner
def get_ascii_banner(center: bool = True) -> str:
    """
    获取 ASCII 艺术风格的横幅，可选择是否居中。
    """
    text = dedent(
        r"""
        ===============================================================================
         __  __       _   _     __  __           _      _                          _   
        |  \/  |     | | | |   |  \/  |         | |    | |   /\                   | |  
        | \  / | __ _| |_| |__ | \  / | ___   __| | ___| |  /  \   __ _  ___ _ __ | |_ 
        | |\/| |/ _` | __| '_ \| |\/| |/ _ \ / _` |/ _ \ | / /\ \ / _` |/ _ \ '_ \| __|
        | |  | | (_| | |_| | | | |  | | (_) | (_| |  __/ |/ ____ \ (_| |  __/ | | | |_ 
        |_|  |_|\__,_|\__|_| |_|_|  |_|\___/ \__,_|\___|_/_/    \_\__, |\___|_| |_|\__|
                                                                    __/ |               
                                                                |___/                
        ===============================================================================
        """,
    ).strip()
    return center_cli_str(text) if center else text
