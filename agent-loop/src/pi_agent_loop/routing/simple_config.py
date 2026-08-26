"""面向业务人员的简化 business.toml 配置。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BusinessConfigError

# 文件内部保留短别名以避免每个校验分支重复冗长名称。
_ConfigError = BusinessConfigError


@dataclass(frozen=True, slots=True)
class SimpleProduct:
    name: str
    description: str
    allow_general_questions: bool


@dataclass(frozen=True, slots=True)
class SimpleIntent:
    id: str
    name: str
    description: str
    examples: tuple[str, ...]
    required_fields: tuple[str, ...]
    capability: str | None
    must_use_tool: bool
    requires_approval: bool
    ask_when_missing: str


@dataclass(frozen=True, slots=True)
class SimpleDeniedRule:
    name: str
    description: str
    examples: tuple[str, ...]
    message: str


@dataclass(frozen=True, slots=True)
class SimpleBusinessConfig:
    product: SimpleProduct
    intents: tuple[SimpleIntent, ...]
    denied: tuple[SimpleDeniedRule, ...] = ()

    def intent_by_id(self, intent_id: str) -> SimpleIntent | None:
        return next(
            (intent for intent in self.intents if intent.id == intent_id),
            None,
        )


_PRODUCT_FIELDS = {"name", "description", "allow_general_questions"}
_INTENT_FIELDS = {
    "id",
    "name",
    "description",
    "examples",
    "required_fields",
    "capability",
    "must_use_tool",
    "requires_approval",
    "ask_when_missing",
}
_DENIED_FIELDS = {"name", "description", "examples", "message"}


def _table(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _ConfigError(f"简化业务配置必须包含 {label}")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _ConfigError(f"{label} 必须是非空字符串")
    return value.strip()


def _texts(
    value: Any,
    label: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise _ConfigError(f"{label} 必须是字符串数组")
    result = tuple(item.strip() for item in value)
    if not result and not allow_empty:
        raise _ConfigError(f"{label} 至少需要一个示例")
    return result


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise _ConfigError(f"{label} 必须是 true 或 false")
    return value


def _parse_intent(raw: Any, index: int) -> SimpleIntent:
    data = _table(raw, f"[[intents]] #{index}")
    unknown = sorted(data.keys() - _INTENT_FIELDS)
    if unknown:
        raise _ConfigError(
            f"Intent #{index} 包含未知字段：{', '.join(unknown)}"
        )
    intent_id = _text(data.get("id"), f"Intent #{index}.id")
    must_use_tool = _boolean(
        data.get("must_use_tool"),
        f"Intent {intent_id}.must_use_tool",
    )
    requires_approval = _boolean(
        data.get("requires_approval", False),
        f"Intent {intent_id}.requires_approval",
    )
    raw_capability = data.get("capability")
    capability = (
        _text(raw_capability, f"Intent {intent_id}.capability")
        if raw_capability is not None
        else None
    )
    if must_use_tool and capability is None:
        raise _ConfigError(
            f"Intent {intent_id} 设置 must_use_tool=true，必须提供 capability"
        )
    if not must_use_tool and capability is not None:
        raise _ConfigError(
            f"Intent {intent_id} 不使用工具，不应配置 capability"
        )
    if requires_approval and not must_use_tool:
        raise _ConfigError(
            f"Intent {intent_id} 需要审批时必须同时使用工具"
        )

    required_fields = _texts(
        data.get("required_fields", []),
        f"Intent {intent_id}.required_fields",
        allow_empty=True,
    )
    ask_when_missing = data.get("ask_when_missing")
    if required_fields:
        ask = _text(
            ask_when_missing,
            f"Intent {intent_id}.ask_when_missing",
        )
    elif ask_when_missing is None:
        ask = "请补充完成该请求所需的信息。"
    else:
        ask = _text(
            ask_when_missing,
            f"Intent {intent_id}.ask_when_missing",
        )

    return SimpleIntent(
        id=intent_id,
        name=_text(data.get("name"), f"Intent {intent_id}.name"),
        description=_text(
            data.get("description"),
            f"Intent {intent_id}.description",
        ),
        examples=_texts(data.get("examples"), f"Intent {intent_id}.examples"),
        required_fields=required_fields,
        capability=capability,
        must_use_tool=must_use_tool,
        requires_approval=requires_approval,
        ask_when_missing=ask,
    )


def _parse_denied(raw: Any, index: int) -> SimpleDeniedRule:
    data = _table(raw, f"[[denied]] #{index}")
    unknown = sorted(data.keys() - _DENIED_FIELDS)
    if unknown:
        raise _ConfigError(
            f"Denied #{index} 包含未知字段：{', '.join(unknown)}"
        )
    name = _text(data.get("name"), f"Denied #{index}.name")
    return SimpleDeniedRule(
        name=name,
        description=_text(
            data.get("description"),
            f"Denied {name}.description",
        ),
        examples=_texts(data.get("examples"), f"Denied {name}.examples"),
        message=_text(data.get("message"), f"Denied {name}.message"),
    )


def load_simple_business_config(path: str | Path) -> SimpleBusinessConfig:
    """读取简化业务配置，拒绝拼写错误和矛盾策略。"""

    config_path = Path(path)
    try:
        with config_path.open("rb") as file:
            root = tomllib.load(file)
    except FileNotFoundError as error:
        raise FileNotFoundError(f"简化业务配置不存在：{config_path}") from error
    except tomllib.TOMLDecodeError as error:
        raise _ConfigError(
            f"简化业务配置 TOML 格式错误：{error}"
        ) from error

    unknown_root = sorted(root.keys() - {"product", "intents", "denied"})
    if unknown_root:
        raise _ConfigError(
            f"简化业务配置包含未知顶层字段：{', '.join(unknown_root)}"
        )
    product_data = _table(root.get("product"), "[product]")
    unknown_product = sorted(product_data.keys() - _PRODUCT_FIELDS)
    if unknown_product:
        raise _ConfigError(
            f"[product] 包含未知字段：{', '.join(unknown_product)}"
        )
    product = SimpleProduct(
        name=_text(product_data.get("name"), "[product].name"),
        description=_text(
            product_data.get("description"),
            "[product].description",
        ),
        allow_general_questions=_boolean(
            product_data.get("allow_general_questions"),
            "[product].allow_general_questions",
        ),
    )

    raw_intents = root.get("intents", [])
    if not isinstance(raw_intents, list) or not raw_intents:
        raise _ConfigError("简化业务配置至少需要一个 [[intents]]")
    intents = tuple(
        _parse_intent(raw, index)
        for index, raw in enumerate(raw_intents, start=1)
    )
    intent_ids = [intent.id for intent in intents]
    if len(intent_ids) != len(set(intent_ids)):
        raise _ConfigError("Intent id 不能重复")

    raw_denied = root.get("denied", [])
    if not isinstance(raw_denied, list):
        raise _ConfigError("[[denied]] 必须是数组表")
    denied = tuple(
        _parse_denied(raw, index)
        for index, raw in enumerate(raw_denied, start=1)
    )
    denied_names = [rule.name for rule in denied]
    if len(denied_names) != len(set(denied_names)):
        raise _ConfigError("Denied name 不能重复")

    return SimpleBusinessConfig(product=product, intents=intents, denied=denied)
