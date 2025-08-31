"""
只获取 .vue 文件内容并写入指定文件（固定扫描 frontend）
"""

# 内置库
import sys
import os
from pathlib import Path

# 要排除的文件夹
IGNORE_FOLDERS = [".git", "__pycache__", "ai-aid-mcmaa", ".venv", "env", "venv", "node_modules", "dist", "build"]

# 要排除的文件
IGNORE_FILES = [
    "vue_contents.py",
]

def generate_directory_structure(startpath, indent="", IGNORE_FOLDERS=None):
    """
    生成目录结构的字符串表示（只展示 .vue 文件）
    """
    structure = ""
    path = Path(startpath)

    try:
        items = sorted(list(path.iterdir()), key=lambda p: (not p.is_dir(), p.name.lower()))
    except FileNotFoundError:
        return structure

    if not items:
        structure += f"{indent}|-- (空目录)\n"
    else:
        for item in items:
            if item.is_dir():
                if IGNORE_FOLDERS and item.name in IGNORE_FOLDERS:
                    continue
                structure += f"{indent}|-- 文件夹: {item.name}\n"
                structure += generate_directory_structure(item, indent + "|   ", IGNORE_FOLDERS)
            else:
                if item.suffix == ".vue" and item.name not in IGNORE_FILES:
                    structure += f"{indent}|-- 文件: {item.name}\n"
    return structure


def clean_content(content):
    """
    清理文本内容：原样返回
    """
    return content


def write_vue_contents_to_file(scan_directory, output_directory, output_file_name, IGNORE_FOLDERS=None):
    """
    仅写入 .vue 文件的内容
    """
    current_dir = Path(scan_directory)

    if not current_dir.is_dir():
        print(f"错误: {current_dir} 不存在或不是目录.")
        return

    # 输出目录
    output_dir = Path(output_directory)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file_path = output_dir / output_file_name

    with open(output_file_path, "w", encoding="utf-8") as output_file:
        # 写目录结构
        directory_structure = generate_directory_structure(current_dir, IGNORE_FOLDERS=IGNORE_FOLDERS)
        output_file.write("目录结构 (仅 .vue 文件):\n")
        output_file.write(directory_structure)
        output_file.write("\n\n")

        # 遍历目录，只处理 .vue 文件
        for root, dirs, files in os.walk(current_dir):
            if IGNORE_FOLDERS:
                dirs[:] = [d for d in dirs if d not in IGNORE_FOLDERS]
            vue_files = [f for f in files if f.endswith(".vue") and f not in IGNORE_FILES]
            vue_files.sort(key=lambda x: x.lower())

            for file in vue_files:
                file_path = Path(root) / file
                try:
                    content = file_path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, IsADirectoryError):
                    try:
                        content = file_path.read_text(encoding="latin1")
                    except Exception:
                        continue

                cleaned_content = clean_content(content)

                marker = "=" * 80
                output_file.write(f"{marker}\n")
                output_file.write(f"{file_path} 的内容:\n")
                output_file.write(f"{marker}\n")
                output_file.write(cleaned_content)
                output_file.write("\n\n")

    print(f"完成：已将 {current_dir} 下的 .vue 内容写入 {output_file_path}")


if __name__ == "__main__":
    # 方式一：基于当前工作目录解析（默认）
    scan_directory = (Path("frontend")).resolve()

    # 如需基于脚本所在目录定位，请改成下面两行：
    # project_root = Path(__file__).resolve().parent
    # scan_directory = (project_root / "frontend").resolve()

    output_directory = "tools/vue_contents/"
    output_file_name = "vue_contents.txt"

    write_vue_contents_to_file(scan_directory, output_directory, output_file_name, IGNORE_FOLDERS)
