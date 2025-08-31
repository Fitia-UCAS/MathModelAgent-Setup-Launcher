import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from app.utils.enums import CompTemplate


def test_get_config_template():
    from app.utils.common_utils import get_config_template

    comp_template = CompTemplate.CHINA
    config_template = get_config_template(comp_template)
    print(config_template)


if __name__ == "__main__":
    test_get_config_template()
