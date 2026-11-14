"""支持 `python -m mes_pipeline.qcsum` 直接调用命令行。"""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
