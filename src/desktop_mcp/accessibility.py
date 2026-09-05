"""A bounded, read-only UIA worker for one explicitly selected foreground window."""

from __future__ import annotations

import argparse
import json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=int, required=True)
    parser.add_argument("--dom", action="store_true")
    arguments = parser.parse_args()
    if arguments.window <= 0:
        parser.error("--window must be a positive window handle")

    import comtypes
    from windows_mcp.desktop.service import Desktop
    from windows_mcp.desktop.utils import remove_private_use_chars, repair_surrogates

    comtypes.CoInitialize()
    try:
        desktop = Desktop()
        state = desktop.tree.get_state(arguments.window, [], use_dom=arguments.dom)
        if not state.status:
            raise RuntimeError("Windows accessibility inspection did not complete successfully.")
        tree = repair_surrogates(remove_private_use_chars(state.semantic_tree_to_string()))
        print(json.dumps({"tree": tree}, ensure_ascii=True))
    finally:
        comtypes.CoUninitialize()


if __name__ == "__main__":
    main()
