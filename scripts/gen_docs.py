#!/usr/bin/env python3
"""
Generate COMMANDS.md from live bot command definitions.

Usage:
    python scripts/gen_docs.py > COMMANDS.md
    python scripts/gen_docs.py --out COMMANDS.md
"""
import sys
import pathlib
import argparse

# Make sure we can import from the project root
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))



def _collect_commands():
    """Walk all cog files and extract command name + docstring pairs."""
    import ast

    cogs_dir = pathlib.Path(__file__).parent.parent / "cogs"
    groups: dict[str, list[tuple[str, str, bool]]] = {}
    # Map command name → (parent_group, docstring, is_subcommand)

    for cog_file in sorted(cogs_dir.glob("*.py")):
        if cog_file.stem.startswith("_"):
            continue
        try:
            src = cog_file.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except Exception:
            continue

        cog_name = cog_file.stem.replace("_", " ").title()
        entries: list[tuple[str, str, bool]] = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            # Look for @commands.hybrid_command or @commands.hybrid_group decorators
            cmd_name = None
            is_group = False
            is_sub = False
            for deco in node.decorator_list:
                # commands.hybrid_command(name="foo")
                if isinstance(deco, ast.Call):
                    func = deco.func
                    fname = ""
                    if isinstance(func, ast.Attribute):
                        fname = func.attr
                    elif isinstance(func, ast.Name):
                        fname = func.id
                    if fname in ("hybrid_command", "hybrid_group", "command"):
                        for kw in deco.keywords:
                            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                                cmd_name = kw.value.value
                        if fname == "hybrid_group":
                            is_group = True

            if not cmd_name:
                continue

            # Get docstring
            doc = ast.get_docstring(node) or ""
            doc = doc.split("\n")[0].strip()  # first line only

            entries.append((cmd_name, doc, is_group))

        if entries:
            groups[cog_name] = entries

    return groups


def generate_markdown(groups: dict) -> str:
    lines = [
        "# Command Reference",
        "",
        "Auto-generated from command definitions. Run `.help <command>` in Discord for full details.",
        "",
    ]

    for cog_name, entries in sorted(groups.items()):
        if cog_name.lower() in ("health", "utils"):
            continue
        lines.append(f"## {cog_name}")
        lines.append("")
        lines.append("| Command | Description |")
        lines.append("|---------|-------------|")
        for cmd_name, doc, is_group in sorted(entries):
            prefix = "🔵 " if is_group else ""
            lines.append(f"| `.{cmd_name}` | {prefix}{doc or ' - '} |")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Generate COMMANDS.md")
    parser.add_argument("--out", default=None, help="Output file (default: stdout)")
    args = parser.parse_args()

    groups = _collect_commands()
    md = generate_markdown(groups)

    if args.out:
        pathlib.Path(args.out).write_text(md, encoding="utf-8")
        print(f"Written to {args.out}", file=sys.stderr)
    else:
        print(md)


if __name__ == "__main__":
    main()
