# -*- coding: utf-8 -*-
"""
将两个目录下的 .py 文件内容，分别汇总到当前目录的 “分支1.txt” 与 “分支2.txt”。

默认目录：
  1) E:\repo1\MathModelAgent-python\backend\app\core  -> 分支1.txt
  2) E:\repo2\MathModelAgent\backend\app\core         -> 分支2.txt

用法（可选地改路径/输出名）：
  python dump_two_branches_py.py \
      --dir1 "E:\\repo1\\MathModelAgent-python\\backend\\app\\core" \
      --dir2 "E:\\repo2\\MathModelAgent\\backend\\app\\core" \
      --out1 "分支1.txt" --out2 "分支2.txt"

说明：
- 仅收集后缀为 .py 的文件；
- 递归扫描，但会排除常见无关目录（如 .git、__pycache__ 等）；
- 自动尝试多种常见编码（utf-8、utf-8-sig、gb18030、latin-1）读取，最大限度避免解码错误；
- 每个文件之间使用分隔线与相对路径标注，便于检索/比对。
"""

import os
import argparse
from pathlib import Path
from datetime import datetime

# 排除的目录名（遇到这些目录名时不进入）
EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    ".idea",
    ".vscode",
    "node_modules",
    "dist",
    "build",
}

# 允许的扩展名（此处只保留 .py）
ALLOW_EXT = {".py"}

# 尝试的文件编码顺序
CANDIDATE_ENCODINGS = ["utf-8", "utf-8-sig", "gb18030", "latin-1"]


def read_text_any_encoding(fp: Path) -> str:
    """
    以多种编码尝试读取文本，返回字符串。
    若全部失败则抛出最后一次异常。
    """
    last_err = None
    for enc in CANDIDATE_ENCODINGS:
        try:
            return fp.read_text(encoding=enc)
        except Exception as e:
            last_err = e
    raise last_err


def iter_py_files(root: Path):
    """
    递归枚举 root 下所有 .py 文件（按相对路径排序）。
    会过滤 EXCLUDE_DIRS。
    """
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        # 过滤排除目录
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]

        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() in ALLOW_EXT:
                files.append(p)

    # 按相对路径排序，保证结果稳定
    files.sort(key=lambda p: p.as_posix())
    return files


def dump_one_dir(root: Path, out_txt: Path):
    """
    将 root 目录下所有 .py 文件内容写入 out_txt。
    """
    if not root.exists() or not root.is_dir():
        print(f"[跳过] 目录不存在或不是目录：{root}")
        return 0

    files = iter_py_files(root)
    count = 0
    sep = "=" * 80

    out_txt.parent.mkdir(parents=True, exist_ok=True)
    with out_txt.open("w", encoding="utf-8", newline="\n") as w:
        header = (
            f"# 汇总时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"# 根目录: {root}\n"
            f"# 文件总数(仅 .py): {len(files)}\n\n"
        )
        w.write(header)

        for fp in files:
            try:
                content = read_text_any_encoding(fp)
            except Exception as e:
                # 某些极端情况仍无法读取，记录并跳过
                w.write(f"{sep}\n# 读取失败: {fp}\n# 错误: {e}\n{sep}\n\n")
                continue

            rel = fp.relative_to(root)
            w.write(f"{sep}\n# 文件: {rel.as_posix()}  (相对 {root})\n{sep}\n")
            w.write(content)
            # 统一以换行结束每个文件块
            if not content.endswith("\n"):
                w.write("\n")
            w.write("\n")
            count += 1

    print(f"[完成] {root} -> {out_txt}  (写入 {count} 个 .py 文件)")
    return count


def main():
    default_dir1 = r"E:\repo1\MathModelAgent-python\backend\app\core"
    default_dir2 = r"E:\repo2\MathModelAgent\backend\app\core"

    parser = argparse.ArgumentParser(description="将两个目录下的 .py 文件分别汇总到两个 txt。")
    parser.add_argument("--dir1", default=default_dir1, help="分支1目录（默认：repo1 的 core）")
    parser.add_argument("--dir2", default=default_dir2, help="分支2目录（默认：repo2 的 core）")
    parser.add_argument("--out1", default="./tools/分支1.txt", help="分支1输出 txt 文件名")
    parser.add_argument("--out2", default="./tools/分支2.txt", help="分支2输出 txt 文件名")
    args = parser.parse_args()

    root1 = Path(args.dir1)
    root2 = Path(args.dir2)
    out1 = Path(args.out1)
    out2 = Path(args.out2)

    n1 = dump_one_dir(root1, out1)
    n2 = dump_one_dir(root2, out2)

    print(f"\n汇总完成：分支1({n1} 个 .py) → {out1}；分支2({n2} 个 .py) → {out2}")


if __name__ == "__main__":
    main()
