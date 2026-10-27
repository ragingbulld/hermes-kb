#!/usr/bin/env python3
"""验证注入插件真的在工作 —— 唯一可信的判据。

不读源码推理，直接调 handle_pre_llm_call，看返回值里有没有
<knowledge-base-titles>。有协议没清单 = 检索断了（最常见是漏装 numpy）。

用法:
    python scripts/verify_injection.py "你库里肯定有的关键词"
    python scripts/verify_injection.py            # 用默认词

环境变量:
    HERMES_KB_CONFIG      配置文件路径
    HERMES_KB_PLUGIN_DIR  插件目录（默认 ~/.hermes/plugins/hermes-kb-auto-inject）
"""
import importlib.util
import os
import pathlib
import sys
import uuid


def load_plugin(plugin_dir: pathlib.Path):
    """按目录加载连字符命名的插件包（目录名不能直接 import）。"""
    init = plugin_dir / "__init__.py"
    if not init.exists():
        sys.exit(f"❌ 插件入口不存在: {init}")

    spec = importlib.util.spec_from_file_location(
        "hermes_kb_auto_inject", init,
        submodule_search_locations=[str(plugin_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermes_kb_auto_inject"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "配置 安装 排查"

    plugin_dir = pathlib.Path(os.environ.get(
        "HERMES_KB_PLUGIN_DIR",
        os.path.expanduser("~/.hermes/plugins/hermes-kb-auto-inject"),
    ))
    print(f"插件目录 : {plugin_dir}")
    print(f"检索词   : {query}")
    print()

    # 顺序很重要：**仓库根目录必须排在最前**，否则 ~/.hermes 里的
    # 同名 hermes_kb 会先被命中，测到的就不是你现在这份代码。
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    sys.path.insert(0, os.path.expanduser("~/.hermes"))
    sys.path.insert(0, str(repo_root))

    try:
        import hermes_kb
        print(f"引擎来源 : {getattr(hermes_kb, '__file__', '?')}")
    except Exception as e:
        print(f"⚠️  引擎导入失败: {type(e).__name__}: {e}")
        print("   → 先在仓库根目录 `pip install -e .`")
    print()

    try:
        mod = load_plugin(plugin_dir)
    except Exception as e:
        print(f"❌ 插件加载失败: {type(e).__name__}: {e}")
        print("   → 依赖是否装在当前解释器环境里？试 `pip install -e .`")
        return 1

    plugin = getattr(mod, "plugin_instance", None)
    if plugin is None:
        print("❌ 插件没有导出 plugin_instance")
        return 1
    print(f"✅ 加载成功: {type(plugin).__name__}")
    print()

    # session_id 必须每次不同：插件按 (session_id, 内容) 做本轮去重，
    # 固定值会让重复运行看起来像「注入没生效」——那是去重，不是故障。
    out = plugin.handle_pre_llm_call(
        session_id=f"verify-{uuid.uuid4()}",
        messages=[{"role": "user", "content": query}],
    )
    ctx = (out or {}).get("context", "") if isinstance(out, dict) else ""

    has_protocol = "【工具调用协议：kb_query】" in ctx
    has_titles = "<knowledge-base-titles>" in ctx

    print(f"返回值类型    : {type(out).__name__}")
    print(f"context 长度  : {len(ctx)}")
    print(f"含协议        : {has_protocol}")
    print(f"★含卡清单      : {has_titles}")
    print()

    if has_titles:
        # 只打印清单那一段，方便肉眼确认命中是否切题
        idx = ctx.find("<knowledge-base-titles>")
        print("── 注入的清单 ──")
        print(ctx[idx:])
        print()
        print("🎉 注入链路正常")
        return 0

    print("❌ 注入没生效：有返回值但没有卡清单。")
    print()
    print("按这个顺序查：")
    print("  1. stderr 有没有 `No module named 'numpy'` → 依赖没装，pip install -e .")
    print("  2. 单独跑 `hermes-kb search \"<同一个词>\"` 确认库是好的")
    print("  3. 报 401/403 → key 没读到，查 HERMES_KB_EMBED_API_KEY")
    print("  4. 上面「引擎来源」指向了别的 hermes_kb → 装了两份，先卸载旧的那份")
    print("  5. 都没问题 → 库里真没相关内容，换个你确定存在的词再试")
    return 1


if __name__ == "__main__":
    sys.exit(main())
