# app/tests/test_common_utils.py

# 1. 标准库与被测函数导入
import unittest
from app.utils.common_utils import split_footnotes


# 2. 测试用例类
# 2.1 目的：验证 split_footnotes 能正确拆分正文与脚注
class TestCommonUtils(unittest.TestCase):
    # 2.1.1 测试：断言 split_footnotes 能正确拆分正文与脚注
    def test_split_footnotes(self):
        # 准备：带脚注的示例文本
        text = "Example[^1]\n\n[^1]: Footnote content"
        # 执行
        main, notes = split_footnotes(text)
        # 断言
        self.assertEqual(main, "Example")
        self.assertEqual(notes, [("1", "Footnote content")])


# 3. 可执行入口
if __name__ == "__main__":
    unittest.main()
