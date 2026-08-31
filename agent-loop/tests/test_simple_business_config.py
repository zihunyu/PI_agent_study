"""简化 business.toml 配置测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    BusinessConfigError,
    load_simple_business_config,
)


class SimpleBusinessConfigTests(unittest.TestCase):
    def test_示例配置不包含正则且可直接读取(self) -> None:
        root = Path(__file__).resolve().parents[1]
        path = root / "config" / "business.toml.example"
        source = path.read_text(encoding="utf-8")

        config = load_simple_business_config(path)

        self.assertNotIn("field_patterns", source)
        self.assertNotIn("patterns =", source)
        self.assertEqual(config.product.name, "订单助手")
        self.assertEqual(len(config.intents), 3)
        query = config.intent_by_id("order.get_status")
        assert query is not None
        self.assertTrue(query.must_use_tool)
        self.assertEqual(query.capability, "orders.read_current")

    def test_must_use_tool_缺少_capability_会被拒绝(self) -> None:
        content = """
[product]
name = "测试"
description = "测试业务"
allow_general_questions = false

[[intents]]
id = "test.read"
name = "读取"
description = "读取数据"
examples = ["读取"]
required_fields = []
must_use_tool = true
requires_approval = false
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "business.toml"
            path.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(BusinessConfigError, "capability"):
                load_simple_business_config(path)

    def test_no_tool_intent_不能偷偷配置_capability(self) -> None:
        content = """
[product]
name = "测试"
description = "测试业务"
allow_general_questions = false

[[intents]]
id = "test.explain"
name = "解释"
description = "解释概念"
examples = ["解释"]
required_fields = []
capability = "secret.read"
must_use_tool = false
requires_approval = false
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "business.toml"
            path.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(BusinessConfigError, "不应配置"):
                load_simple_business_config(path)

    def test_支持optional_fields多capability和显式风险策略(self) -> None:
        content = """
[product]
name = "采购"
description = "采购业务"
allow_general_questions = false

[[intents]]
id = "purchase.create"
name = "创建采购"
description = "预占库存并创建采购"
examples = ["购买商品"]
required_fields = ["product_id"]
optional_fields = ["note", "tags"]
capabilities = ["inventory.reserve", "purchases.create"]
must_use_tool = true
requires_approval = false
side_effect = true
risk = "high"
ask_when_missing = "请提供商品"
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "business.toml"
            path.write_text(content, encoding="utf-8")

            config = load_simple_business_config(path)

        intent = config.intents[0]
        self.assertEqual(intent.capability, None)
        self.assertEqual(
            intent.required_capabilities,
            ("inventory.reserve", "purchases.create"),
        )
        self.assertEqual(intent.optional_fields, ("note", "tags"))
        self.assertTrue(intent.has_side_effect)
        self.assertEqual(intent.risk, "high")

    def test_required与optional字段重复时拒绝(self) -> None:
        content = """
[product]
name = "测试"
description = "测试业务"
allow_general_questions = false

[[intents]]
id = "test.read"
name = "读取"
description = "读取数据"
examples = ["读取"]
required_fields = ["resource_id"]
optional_fields = ["resource_id"]
capability = "resources.read"
must_use_tool = true
requires_approval = false
ask_when_missing = "请提供资源"
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "business.toml"
            path.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(BusinessConfigError, "重复"):
                load_simple_business_config(path)


if __name__ == "__main__":
    unittest.main()
