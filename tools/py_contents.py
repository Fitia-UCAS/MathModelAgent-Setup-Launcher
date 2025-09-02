# 内置库
import sys
import os
from pathlib import Path

# 要排除的文件夹
IGNORE_FOLDERS = [".git", "__pycache__", "ai-aid-mcmaa", ".venv", "env", "venv"]

# 要排除的文件
IGNORE_FILES = [
    "py_contents.py",
    "__init__.py",
]


def generate_directory_structure(startpath, indent="", IGNORE_FOLDERS=None):
    """
    生成目录结构的字符串表示（只展示 .py 文件）
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
                if item.suffix == ".py" and item.name not in IGNORE_FILES:
                    structure += f"{indent}|-- 文件: {item.name}\n"
    return structure


def clean_content(content):
    """
    清理文本内容：原样返回
    """
    return content


def get_next_output_filename(output_dir: Path, base_name: str, ext: str = ".txt") -> Path:
    """
    根据现有文件自动生成下一个编号的文件名
    例如：后端现有的项目源码_001.txt, 后端现有的项目源码_002.txt ...
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    i = 1
    while True:
        filename = f"{base_name}_{i:03d}{ext}"
        candidate = output_dir / filename
        if not candidate.exists():
            return candidate
        i += 1


def write_py_contents_to_file(scan_directory, output_directory, base_file_name, IGNORE_FOLDERS=None):
    """
    仅写入 .py 文件的内容，并自动编号输出
    """
    current_dir = Path(scan_directory)

    if not current_dir.is_dir():
        print(f"错误: {current_dir} 不存在或不是目录.")
        return

    # 获取输出文件路径（自动编号）
    output_dir = Path(output_directory)
    output_file_path = get_next_output_filename(output_dir, base_file_name, ".txt")

    with open(output_file_path, "w", encoding="utf-8") as output_file:
        # 写目录结构
        directory_structure = generate_directory_structure(current_dir, IGNORE_FOLDERS=IGNORE_FOLDERS)
        output_file.write("目录结构 (仅 .py 文件):\n")
        output_file.write(directory_structure)
        output_file.write("\n\n")

        # 遍历目录，只处理 .py 文件
        for root, dirs, files in os.walk(current_dir):
            if IGNORE_FOLDERS:
                dirs[:] = [d for d in dirs if d not in IGNORE_FOLDERS]
            py_files = [f for f in files if f.endswith(".py") and f not in IGNORE_FILES]
            py_files.sort(key=lambda x: x.lower())

            for file in py_files:
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

    print(f"完成：已将 {current_dir} 下的 .py 内容写入 {output_file_path}")


if __name__ == "__main__":
    scan_directory = Path("backend") / "app"
    scan_directory = scan_directory.resolve()

    output_directory = "tools/"
    base_file_name = "后端现有的项目源码"

    write_py_contents_to_file(scan_directory, output_directory, base_file_name, IGNORE_FOLDERS)
