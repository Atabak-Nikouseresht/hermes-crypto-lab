"""Fail-closed source scan for prohibited private exchange and credential paths."""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

PROHIBITED_METHODS = {
    "order creation method": frozenset(
        {
            "create_order",
            "create_orders",
            "create_market_buy_order",
            "create_market_sell_order",
            "create_limit_buy_order",
            "create_limit_sell_order",
        }
    ),
    "private order-management method": frozenset(
        {
            "edit_order",
            "cancel_order",
            "cancel_orders",
            "cancel_all_orders",
            "fetch_order",
            "fetch_orders",
            "fetch_open_orders",
            "fetch_closed_orders",
            "fetch_my_trades",
        }
    ),
    "private account or position method": frozenset(
        {
            "fetch_accounts",
            "fetch_balance",
            "fetch_position",
            "fetch_positions",
        }
    ),
    "withdrawal or transfer method": frozenset(
        {"withdraw", "withdrawal", "transfer"}
    ),
    "leverage or margin method": frozenset(
        {
            "set_leverage",
            "set_margin_mode",
            "set_position_mode",
            "borrow_margin",
            "repay_margin",
        }
    ),
}
PRIVATE_ENDPOINT_PREFIXES = ("private_", "sapi_", "fapi_", "dapi_")
PRIVATE_CREDENTIAL_ASSIGNMENT = re.compile(
    r"\b(?:apiKey|api_key|apiSecret|api_secret|BINANCE_API_KEY|BINANCE_SECRET)\s*="
)


def _snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _constant_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _constant_string(node.left)
        right = _constant_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _prohibited_category(method_name: str) -> str | None:
    method = _snake_case(method_name)
    for category, methods in PROHIBITED_METHODS.items():
        if method in methods:
            return category
    if method.startswith("create_") and method.endswith(("order", "orders")):
        return "order creation method"
    if method.startswith(("edit_", "cancel_", "fetch_")) and "order" in method:
        return "private order-management method"
    if method.startswith("fetch_") and any(
        term in method for term in ("account", "balance", "position", "my_trade")
    ):
        return "private account or position method"
    if method.startswith(("withdraw", "transfer")):
        return "withdrawal or transfer method"
    if method.startswith(("set_", "borrow_", "repay_")) and any(
        term in method for term in ("leverage", "margin", "position_mode")
    ):
        return "leverage or margin method"
    if method.startswith(PRIVATE_ENDPOINT_PREFIXES):
        return "private exchange endpoint"
    return None


def scan(root: Path) -> list[tuple[str, str, int]]:
    findings: list[tuple[str, str, int]] = []
    candidates = [root / "src", root / "scripts"] + [
        root / name
        for name in ("run_paper.py", "run_monthly_report.py", "run_data_pipeline.py")
    ]
    files = []
    for candidate in candidates:
        if candidate.is_dir():
            files.extend(candidate.rglob("*.py"))
        elif candidate.is_file():
            files.append(candidate)
    scanner_path = (root / "scripts" / "verify_safety.py").resolve()
    files = [path for path in files if path.resolve() != scanner_path]
    for path in sorted(files):
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if PRIVATE_CREDENTIAL_ASSIGNMENT.search(line):
                findings.append(
                    (
                        "private credential assignment",
                        str(path.relative_to(root)).replace("\\", "/"),
                        line_number,
                    )
                )
        relative_path = str(path.relative_to(root)).replace("\\", "/")
        try:
            tree = ast.parse(text, filename=relative_path)
        except SyntaxError as error:
            findings.append(("unparseable Python source", relative_path, error.lineno or 1))
            continue
        for node in ast.walk(tree):
            method_name = None
            if isinstance(node, ast.Attribute):
                method_name = node.attr
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
            ):
                method_name = _constant_string(node.args[1])
            if method_name is None:
                continue
            category = _prohibited_category(method_name)
            if category is not None:
                findings.append((category, relative_path, node.lineno))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    findings = scan(args.root.resolve())
    if findings:
        for category, path, line in findings:
            print(f"PROHIBITED category={category} path={path} line={line}")
        return 1
    print("PASS: no prohibited private exchange or credential path found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
