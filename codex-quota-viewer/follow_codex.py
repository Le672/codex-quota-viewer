"""无 Qt 的 Windows 联动程序；源码与查看器共用进程识别和命名事件。"""
import argparse
import os
import sys


def main():
    for name in ('stdout', 'stderr'):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, 'w', encoding='utf-8'))
    parser = argparse.ArgumentParser(description='随 Codex 桌面启动 / 退出额度查看器')
    parser.add_argument('--watch-codex', action='store_true', help='后台等待 Codex 桌面')
    parser.parse_args()
    from codexquota.lifecycle import watch_codex
    from codexquota.appenv import log
    try:
        return watch_codex()
    except Exception as exc:
        log(f'Codex 桌面联动异常: {type(exc).__name__}: {exc}')
        return 3


if __name__ == '__main__':
    sys.exit(main())
