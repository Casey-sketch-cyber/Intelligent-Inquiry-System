"""安装脚本调用的设备配对步骤；不会执行 QQ、格式化磁盘或写入登录态。"""
import argparse
from pathlib import Path
from lanhost import config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("office", "worker", "verify-worker"))
    parser.add_argument("--bootstrap", type=Path)
    args = parser.parse_args()
    if args.role == "office":
        config.controller()
    elif args.role == "verify-worker":
        config.worker_config()
    else:
        if not args.bootstrap:
            raise ValueError("缺少办公电脑部署资料")
        config.prepare_worker(config.read_json(args.bootstrap))
    print("设备角色核验完成；保持禁止真实发送。")


if __name__ == "__main__":
    main()
