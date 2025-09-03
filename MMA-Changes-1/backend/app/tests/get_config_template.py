# app/tests/get_config_template.py

# 1. 路径与标准库导入
import sys
import os

# 1.1 将项目根目录加入 sys.path（以便导入 app 包）
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

# 2. 业务枚举导入
from app.schemas.enums import CompTemplate


# 3. 测试函数
# 3.1 目的：检验 get_config_template 能返回指定模板配置并打印
def test_get_config_template():
    # 3.1.1 导入工具函数并调用
    from app.utils.common_utils import get_config_template

    comp_template = CompTemplate.CHINA
    config_template = get_config_template(comp_template)
    print(config_template)


# 4. 可执行入口
if __name__ == "__main__":
    test_get_config_template()
