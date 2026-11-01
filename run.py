#!/usr/bin/env python3
"""Codex 额度查看器 —— 入口。

    python run.py                 # 打开额度查看窗口
    python run.py --minimized     # 直接后台运行（托盘常驻）
    python run.py --selftest      # 离线自检（打印解析/分析结果）
    python run.py --selftest --live   # 自检并真实调用一次官方接口
    python run.py --json          # 打印一次 JSON 结果后退出（不建窗口）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def _prepare_stdio() -> None:
    """windowed 打包时 stdout/stderr 可能是 None，避免 print 崩溃。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except Exception:
                pass
    try:
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
def _hide_console_if_frozen() -> None:
    """单文件 windowed 模式下，尽量避免出现多余的控制台窗口。"""
    if not getattr(sys, "frozen", False):
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        handle = kernel32.GetConsoleWindow()
        if handle:
            # 只有在没有附加到父控制台时才隐藏
            user32.ShowWindow(handle, 0)
    except Exception:
        pass


def _fetch_once(output=None) -> int:
    """不建窗口，采集一次并输出 JSON。"""
    from codexquota import analytics as ana
    from codexquota.appenv import Settings, log
    from codexquota.collector import DataController
    import queue

    settings = Settings.load()
    events: "queue.Queue[dict]" = queue.Queue()
    controller = DataController(settings, events)
    controller.refresh_async("cli")
    bundle = None
    deadline = time.monotonic() + 90
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            sys.stderr.write("采集超时，请检查网络或使用本地模式。\n")
            return 4
        try:
            event = events.get(timeout=remaining)
        except queue.Empty:
            sys.stderr.write("采集超时。\n")
            return 4
        if event.get("type") == "refresh_done":
            bundle = event["bundle"]
            break
    assert bundle is not None
    from codexquota.pricing import estimate_value
    payload = {
        "snapshot": bundle.snapshot.to_dict(),
        "advice": bundle.advice.to_dict(),
        "samples": len(bundle.samples),
        "elapsed_ms": round(bundle.elapsed * 1000, 1),
        "source_note": bundle.source_note,
        "community": bundle.community.to_dict() if bundle.community else None,
        "tokens": bundle.tokens.to_dict(),
        "token_values": {kind: estimate_value(estimate, settings.pricing_model).to_dict()
                         for kind, estimate in bundle.tokens.windows.items()},
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if output or getattr(sys, "frozen", False):
        from pathlib import Path
        from codexquota.appenv import app_data_dir
        destination = Path(output) if output else app_data_dir() / "last-snapshot.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text + "\n", encoding="utf-8")
        log(f"JSON 快照已保存: {destination}")
    try:
        sys.stdout.write(text + "\n")
    except Exception:
        log("JSON 输出失败")
    return 0 if bundle.snapshot.ok else 2


def main(argv=None) -> int:
    _prepare_stdio()
    parser = argparse.ArgumentParser(
        prog="codex-quota-viewer",
        description="Codex 额度 / 重置卡查看器（托盘常驻）",
    )
    parser.add_argument("--minimized", action="store_true", help="启动后直接最小化到托盘")
    parser.add_argument("--watch-codex", action="store_true", help="后台联动：随 Codex 桌面启动和退出")
    parser.add_argument("--follow-codex", action="store_true", help="由 Codex 联动器打开窗口")
    parser.add_argument("--selftest", action="store_true", help="运行离线自检")
    parser.add_argument("--live", action="store_true", help="自检时真实调用官方接口")
    parser.add_argument("--json", action="store_true", help="输出一次 JSON 结果后退出")
    parser.add_argument("--output", help="--json 的输出文件路径（EXE 默认保存到数据目录）")
    parser.add_argument("--debug", action="store_true", help="输出调试日志到控制台")
    parser.add_argument("--screenshot", help="首次刷新完成后将当前窗口保存为 PNG（用于检查界面）")
    args = parser.parse_args(argv)

    if args.debug:
        os.environ["CODEXQUOTA_DEBUG"] = "1"

    if args.selftest:
        from codexquota.selftest import run_selftest

        return run_selftest(live=args.live)

    if args.json:
        return _fetch_once(args.output)

    if args.watch_codex:
        from codexquota.lifecycle import ensure_registered_follower
        if ensure_registered_follower():
            return 0
        if getattr(sys, "frozen", False):
            from pathlib import Path
            follower = Path(sys.executable).with_name("Codex额度联动.exe")
            if follower.is_file():
                import subprocess
                environment = os.environ.copy()
                environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
                subprocess.Popen([str(follower), "--watch-codex"], env=environment,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
                return 0
        from codexquota.lifecycle import watch_codex
        return watch_codex()

    try:
        import PySide6.QtWidgets  # noqa: F401
    except Exception as exc:
        sys.stderr.write(
            "无法加载 Qt：" + repr(exc) + "\n"
            "源码运行需要 PySide6-Essentials；打包 EXE 已包含运行库。\n"
        )
        return 3

    from codexquota.instance import SingleInstance
    from codexquota.lifecycle import ensure_registered_follower
    ensure_registered_follower()
    instance = SingleInstance()
    if not instance.acquire():
        return 0
    from codexquota.appenv import Settings, log, platform_summary
    from codexquota.ui import QuotaApp

    log(f"启动 {platform_summary()}")
    settings = Settings.load()
    startup_minimized = (False if args.follow_codex or args.screenshot else
                         True if args.minimized else None)
    app = QuotaApp(settings, start_minimized=startup_minimized)
    if args.screenshot:
        from pathlib import Path
        from PySide6.QtCore import QTimer
        capture_timer = QTimer(app.root)
        capture_started = time.monotonic()
        def capture():
            if not app.bundle.fetched_at and time.monotonic() - capture_started < 60:
                return
            capture_timer.stop()
            destination = Path(args.screenshot)
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not app.root.grab().save(str(destination), "PNG"):
                    raise OSError("PNG 保存失败")
                log(f"窗口预览已保存: {destination}")
            except OSError as exc:
                log(f"保存窗口预览失败: {exc}")
        capture_timer.timeout.connect(capture)
        capture_timer.start(500)
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            app.quit_app()
        except Exception:
            pass
        instance.release()
    return 0


if __name__ == "__main__":
    _hide_console_if_frozen()
    sys.exit(main())
