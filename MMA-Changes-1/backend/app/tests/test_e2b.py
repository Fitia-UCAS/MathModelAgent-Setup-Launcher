# app/tests/test_e2b.py

# 1 环境与标准库
# 1.1 常用：文件、异步、单元测试
import os
import asyncio
import unittest

# 2 可选依赖与回退策略
# 2.1 dotenv：用于加载环境变量（若不存在则提供回退实现）
try:
    from dotenv import load_dotenv
except ModuleNotFoundError:

    def load_dotenv(*args, **kwargs):
        return None


# 2.2 E2B 解释器：若未安装则设置为 None（测试将跳过）
try:
    from app.tools.e2b_interpreter import E2BCodeInterpreter
except ModuleNotFoundError:
    E2BCodeInterpreter = None

# 3 项目工具函数
# 3.1 create_task_id / create_work_dir：用于创建任务 ID 与工作目录
from app.utils.common_utils import create_task_id, create_work_dir

# 3.2 NotebookSerializer（用于构造笔记本环境）
from app.tools.notebook_serializer import NotebookSerializer


# 4 测试类
# 4.1 测试目的：验证 E2BCodeInterpreter 在存在 E2B_API_KEY 时能初始化并执行代码
class TestE2BCodeInterpreter(unittest.TestCase):
    def setUp(self):
        # 4.1.1 加载环境变量（若支持）
        load_dotenv()

        # 4.1.2 解释器不可用则跳过测试
        if E2BCodeInterpreter is None:
            self.skipTest("e2b_code_interpreter not available")

        # 4.1.3 创建工作目录并构造 NotebookSerializer（测试用固定 ID）
        _, dirs = create_work_dir("20250312-104132-d3625cab")
        notebook = NotebookSerializer(dirs["jupyter"])

        # 4.1.4 初始化解释器实例（注意：self.task_id 与 self.work_dir 需由测试框架或调用方提供）
        self.code_interpreter = E2BCodeInterpreter(self.task_id, self.work_dir, notebook)

    def test_execute_code(self):
        # 4.2.1 前提：需要环境变量 E2B_API_KEY；否则跳过
        if not os.getenv("E2B_API_KEY"):
            self.skipTest("E2B_API_KEY not set")

        # 4.2.2 待执行的示例代码（简单绘图）
        code = """
import matplotlib.pyplot as plt
import numpy as np

# 生成数据
x = np.linspace(0, 2 * np.pi, 100)  # x从0到2π，生成100个点
y = np.sin(x)                       # 计算对应的sin(x)值

# 绘图
plt.figure(figsize=(8, 4))          # 设置画布大小
plt.plot(x, y, label='y = sin(x)')  # 绘制曲线，并添加图例

# 添加标签和标题
plt.title("Simple Sine Function")
plt.xlabel("x")
plt.ylabel("y")

# 添加网格和图例
plt.grid(True)
plt.legend()

# 显示图像
plt.show()
"""
        # 4.2.3 初始化解释器并执行代码
        asyncio.run(self.code_interpreter.initialize())
        asyncio.run(self.code_interpreter.execute_code(code))


# 5 可执行入口
if __name__ == "__main__":
    unittest.main()
